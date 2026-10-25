"""`ChainDataSource` backed by node providers.

Standard Ethereum JSON-RPC (any provider) supplies balance, contract status and nonce.
Plain JSON-RPC has no index by address, so address history (first/last seen, recent
transfers) comes from Alchemy's enhanced Transfers API (`alchemy_getAssetTransfers`).
Contract names are not available from either; `FallbackChainData` can enrich them from
Etherscan.

Provider URLs often embed an API key, so no error message or log line here contains a
URL, and HTTP errors are raised `from None` to keep the URL out of tracebacks.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Literal, Self

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.core.schemas import AddressProfile, Chain, Transfer, normalize_address
from sentinel.tools._http import RateLimiter, RetryableError, request_with_retry

ALCHEMY_URLS: dict[Chain, str] = {Chain.ETHEREUM: "https://eth-mainnet.g.alchemy.com/v2/"}

# Alchemy returns at most 1000 (0x3e8) transfers per page.
ALCHEMY_PAGE_SIZE = 1000
# Same cap as `EtherscanChainData`, so callers see one limit whatever the source.
MAX_RECORDS = 10_000

HISTORY_CATEGORIES: tuple[str, ...] = ("external", "internal", "erc20")
_NATIVE_CATEGORIES = frozenset({"external", "internal"})

# JSON-RPC error codes providers use for rate limits (Alchemy 429, Infura-style -32005).
_RATE_LIMIT_CODES = frozenset({429, -32005})

Direction = Literal["from", "to"]
Order = Literal["asc", "desc"]


class RpcError(RuntimeError):
    """A node provider returned an error that retrying will not fix."""


class HistoryNotConfiguredError(RpcError):
    """Address history needs an indexed provider (Alchemy) and none is configured."""


class RpcSettings(BaseSettings):
    """Node-provider settings (env prefix `SENTINEL_`), kept here so core config stays small."""

    model_config = SettingsConfigDict(env_prefix="SENTINEL_", env_file=".env", extra="ignore")

    eth_rpc_url: SecretStr | None = None
    alchemy_api_key: SecretStr | None = None


def _secret(value: str | SecretStr | None) -> str | None:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    return value or None


def _is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(s in lowered for s in ("rate limit", "too many", "capacity"))


class JsonRpcClient:
    """Minimal async JSON-RPC 2.0 client with rate limiting and retries.

    `name` identifies the provider in errors instead of the (possibly secret) URL.
    """

    def __init__(
        self,
        url: str | SecretStr,
        *,
        name: str = "JSON-RPC",
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        rate_per_second: float = 10.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
    ) -> None:
        resolved = _secret(url)
        if not resolved:
            raise RpcError(f"{name}: a provider URL is required")
        self._url = resolved
        self.name = name
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._limiter = RateLimiter(rate_per_second)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._ids = itertools.count(1)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        """Send one request and return its `result`."""
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}

        async def send() -> httpx.Response:
            await self._limiter.wait()
            return await self._client.post(self._url, json=payload)

        try:
            response = await request_with_retry(
                send,
                max_retries=self._max_retries,
                backoff_base=self._backoff_base,
                check=self._check_body,
            )
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise RpcError(f"{self.name} HTTP {status} for {method}") from None
        except httpx.TransportError as exc:
            raise RpcError(f"{self.name} unreachable for {method}: {type(exc).__name__}") from None
        except RetryableError as exc:
            raise RpcError(f"{self.name} rate limit persisted for {method}: {exc}") from None
        except RpcError as exc:
            raise RpcError(f"{exc} (method {method})") from None
        return response.json()["result"]

    def _check_body(self, response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError:
            raise RpcError(f"{self.name} returned a non-JSON body") from None
        if not isinstance(body, dict):
            raise RpcError(f"{self.name} returned an unexpected body: {str(body)[:200]}")
        error = body.get("error")
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = str(error.get("message", error) if isinstance(error, dict) else error)
            if code in _RATE_LIMIT_CODES or _is_rate_limited(message):
                raise RetryableError(message)
            raise RpcError(f"{self.name} error {code}: {message}")
        if "result" not in body:
            raise RpcError(f"{self.name} returned no result: {str(body)[:200]}")


class AlchemyTransfers:
    """Alchemy's `alchemy_getAssetTransfers`, paginated.

    https://www.alchemy.com/docs/reference/alchemy-getassettransfers
    """

    def __init__(self, rpc: JsonRpcClient) -> None:
        self._rpc = rpc

    async def fetch(
        self,
        address: str,
        *,
        direction: Direction,
        order: Order,
        limit: int,
        categories: Sequence[str] = HISTORY_CATEGORIES,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` raw transfers sent from or received by `address`."""
        transfers: list[dict[str, Any]] = []
        page_key: str | None = None
        while len(transfers) < limit:
            params: dict[str, Any] = {
                "fromBlock": "0x0",
                "toBlock": "latest",
                f"{direction}Address": address,
                "category": list(categories),
                "order": order,
                "withMetadata": True,
                # Zero-value token transfers are an address-poisoning signal; keep them.
                "excludeZeroValue": False,
                "maxCount": hex(min(limit - len(transfers), ALCHEMY_PAGE_SIZE)),
            }
            if page_key:
                params["pageKey"] = page_key
            result = await self._rpc.call("alchemy_getAssetTransfers", [params])
            if not isinstance(result, dict):
                raise RpcError(f"{self._rpc.name} returned an unexpected transfers result")
            transfers.extend(result.get("transfers") or [])
            page_key = result.get("pageKey") or None
            if page_key is None:
                break
        return transfers[:limit]


def _alchemy_timestamp(row: dict[str, Any]) -> datetime:
    value = (row.get("metadata") or {}).get("blockTimestamp")
    if not value:
        raise RpcError("Alchemy transfer has no metadata.blockTimestamp")
    return datetime.fromisoformat(str(value))


def _alchemy_amount(row: dict[str, Any], native: bool) -> float:
    raw = row.get("rawContract") or {}
    raw_value, raw_decimal = raw.get("value"), raw.get("decimal")
    if raw_value is not None:
        decimals = int(raw_decimal, 16) if raw_decimal else (18 if native else 0)
        return float(Decimal(int(raw_value, 16)) / (Decimal(10) ** decimals))
    # No raw value (unusual for the categories we request): use Alchemy's scaled value.
    return float(row.get("value") or 0)


def _alchemy_transfer(chain: Chain, row: dict[str, Any]) -> Transfer:
    native = row.get("category") in _NATIVE_CATEGORIES
    contract = ((row.get("rawContract") or {}).get("address") or "").lower()
    return Transfer(
        chain=chain,
        tx_hash=row["hash"],
        block_number=int(row["blockNum"], 16),
        timestamp=_alchemy_timestamp(row),
        from_address=(row.get("from") or "").lower(),
        to_address=(row.get("to") or "").lower(),
        asset="ETH" if native else (row.get("asset") or contract),
        amount=_alchemy_amount(row, native),
    )


def _dedupe_key(row: dict[str, Any]) -> tuple[Any, ...]:
    # A self-transfer appears in both the "from" and "to" queries.
    if row.get("uniqueId"):
        return (row["uniqueId"],)
    raw = row.get("rawContract") or {}
    return (row.get("hash"), row.get("category"), row.get("from"), row.get("to"), raw.get("value"))


class RpcChainData:
    """Node-provider adapter. Use as an async context manager, or call `aclose()`.

    `rpc_url` is any Ethereum mainnet JSON-RPC endpoint. `alchemy_api_key` enables
    history; when `rpc_url` is omitted the Alchemy endpoint serves JSON-RPC too.
    """

    def __init__(
        self,
        rpc_url: str | SecretStr | None = None,
        *,
        alchemy_api_key: str | SecretStr | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        rate_per_second: float = 10.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
    ) -> None:
        url, key = _secret(rpc_url), _secret(alchemy_api_key)
        if url is None and key is None:
            raise RpcError(
                "a node provider is required (SENTINEL_ETH_RPC_URL or SENTINEL_ALCHEMY_API_KEY)"
            )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        options: dict[str, Any] = {
            "client": self._client,
            "rate_per_second": rate_per_second,
            "max_retries": max_retries,
            "backoff_base": backoff_base,
        }
        alchemy = (
            JsonRpcClient(ALCHEMY_URLS[Chain.ETHEREUM] + key, name="Alchemy", **options)
            if key
            else None
        )
        rpc = JsonRpcClient(url, **options) if url else alchemy
        if rpc is None:  # unreachable: one of url / key is set
            raise RpcError("a node provider is required")
        self._rpc: JsonRpcClient = rpc
        self._history = AlchemyTransfers(alchemy) if alchemy else None

    @classmethod
    def from_settings(cls, settings: RpcSettings | None = None, **kwargs: Any) -> Self:
        settings = settings or RpcSettings()
        if settings.eth_rpc_url is None and settings.alchemy_api_key is None:
            raise RpcError("neither SENTINEL_ETH_RPC_URL nor SENTINEL_ALCHEMY_API_KEY is set")
        return cls(settings.eth_rpc_url, alchemy_api_key=settings.alchemy_api_key, **kwargs)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # -- ChainDataSource ---------------------------------------------------------------

    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        """Profile an address.

        Same semantics as `EtherscanChainData`: `tx_count` is the nonce, and
        `first_seen` / `last_seen` cover external, internal and ERC-20 transfers in and
        out. Labels are `["contract"]` for contracts, without a name.
        """
        address = normalize_address(address)
        history = self._require_history(chain)
        balance, code, nonce = await asyncio.gather(
            self._rpc.call("eth_getBalance", [address, "latest"]),
            self._rpc.call("eth_getCode", [address, "latest"]),
            self._rpc.call("eth_getTransactionCount", [address, "latest"]),
        )
        is_contract = str(code) not in ("0x", "0x0", "")

        edges = await asyncio.gather(
            *(
                history.fetch(address, direction=direction, order=order, limit=1)
                for direction in ("from", "to")
                for order in ("asc", "desc")
            )
        )
        stamps = [_alchemy_timestamp(rows[0]) for rows in edges if rows]

        return AddressProfile(
            chain=chain,
            address=address,
            is_contract=is_contract,
            balance_wei=int(balance, 16),
            tx_count=int(nonce, 16),
            first_seen=min(stamps) if stamps else None,
            last_seen=max(stamps) if stamps else None,
            labels=["contract"] if is_contract else [],
        )

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        address = normalize_address(address)
        history = self._require_history(chain)
        if limit <= 0:
            return []
        limit = min(limit, MAX_RECORDS)
        sent, received = await asyncio.gather(
            history.fetch(address, direction="from", order="desc", limit=limit),
            history.fetch(address, direction="to", order="desc", limit=limit),
        )
        seen: set[tuple[Any, ...]] = set()
        transfers: list[Transfer] = []
        for row in [*sent, *received]:
            key = _dedupe_key(row)
            if key in seen:
                continue
            seen.add(key)
            transfers.append(_alchemy_transfer(chain, row))
        transfers.sort(key=lambda t: (t.block_number, t.timestamp), reverse=True)
        return transfers[:limit]

    # -- helpers -----------------------------------------------------------------------

    def _require_history(self, chain: Chain) -> AlchemyTransfers:
        if chain not in ALCHEMY_URLS:
            raise RpcError(f"unsupported chain: {chain}")
        if self._history is None:
            raise HistoryNotConfiguredError(
                "history provider not configured (set SENTINEL_ALCHEMY_API_KEY)"
            )
        return self._history
