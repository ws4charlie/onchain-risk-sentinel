"""`ChainDataSource` backed by the Etherscan API V2 (https://docs.etherscan.io/).

One endpoint serves every supported chain through the `chainid` parameter. The free
tier allows about 5 calls per second; this adapter spaces calls and retries on HTTP
429/5xx and on Etherscan's in-body rate-limit message (which arrives as HTTP 200).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import SecretStr

from sentinel.core.config import Settings
from sentinel.core.schemas import AddressProfile, Chain, Transfer, normalize_address
from sentinel.tools._http import RateLimiter, RetryableError, request_with_retry

BASE_URL = "https://api.etherscan.io/v2/api"
CHAIN_IDS: dict[Chain, int] = {Chain.ETHEREUM: 1}

# Etherscan caps page * offset at 10,000 records per query.
MAX_RECORDS = 10_000

_NO_RECORDS = ("no transactions found", "no records found")


class EtherscanError(RuntimeError):
    """Etherscan returned an error that retrying will not fix (bad key, bad params, ...)."""


def _is_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return "rate limit" in lowered or "too many" in lowered


def _ts(value: str) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _scaled(raw: str, decimals: int) -> float:
    return float(Decimal(raw) / (Decimal(10) ** decimals))


class EtherscanChainData:
    """Etherscan V2 adapter. Use as an async context manager, or call `aclose()`."""

    def __init__(
        self,
        api_key: str | SecretStr,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        rate_per_second: float = 4.5,
        max_retries: int = 4,
        backoff_base: float = 0.5,
    ) -> None:
        key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        if not key:
            raise EtherscanError("an Etherscan API key is required (SENTINEL_ETHERSCAN_API_KEY)")
        self._key = key
        self._base_url = base_url
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout))
        self._limiter = RateLimiter(rate_per_second)
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    @classmethod
    def from_settings(cls, settings: Settings | None = None, **kwargs: Any) -> Self:
        settings = settings or Settings()
        if settings.etherscan_api_key is None:
            raise EtherscanError("SENTINEL_ETHERSCAN_API_KEY is not set")
        return cls(settings.etherscan_api_key, **kwargs)

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

        `tx_count` is the account nonce (transactions sent by the address), because the
        free API has no total-count endpoint. `first_seen` / `last_seen` cover normal,
        internal and ERC-20 transfers, so receive-only addresses are dated too.
        """
        address = normalize_address(address)
        balance = int(await self._call(chain, module="account", action="balance", address=address))
        code = await self._proxy(chain, "eth_getCode", address=address, tag="latest")
        nonce = await self._proxy(chain, "eth_getTransactionCount", address=address, tag="latest")
        is_contract = code not in ("0x", "0x0", "")

        first: list[datetime] = []
        last: list[datetime] = []
        for action in ("txlist", "txlistinternal", "tokentx"):
            oldest = await self._records(chain, action, address, limit=1, newest_first=False)
            if not oldest:
                continue
            newest = await self._records(chain, action, address, limit=1, newest_first=True)
            first.append(_ts(oldest[0]["timeStamp"]))
            last.append(_ts((newest or oldest)[0]["timeStamp"]))

        labels: list[str] = []
        if is_contract:
            labels.append(await self._contract_label(chain, address))

        return AddressProfile(
            chain=chain,
            address=address,
            is_contract=is_contract,
            balance_wei=balance,
            tx_count=int(nonce, 16),
            first_seen=min(first) if first else None,
            last_seen=max(last) if last else None,
            labels=labels,
        )

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        address = normalize_address(address)
        if limit <= 0:
            return []
        limit = min(limit, MAX_RECORDS)
        transfers: list[Transfer] = []
        for row in await self._records(chain, "txlist", address, limit=limit):
            if row.get("isError") == "1":
                continue
            transfers.append(self._native(chain, row))
        for row in await self._records(chain, "txlistinternal", address, limit=limit):
            if row.get("isError") == "1":
                continue
            transfers.append(self._native(chain, row))
        for row in await self._records(chain, "tokentx", address, limit=limit):
            transfers.append(self._token(chain, row))
        transfers.sort(key=lambda t: (t.block_number, t.timestamp), reverse=True)
        return transfers[:limit]

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _native(chain: Chain, row: dict[str, Any]) -> Transfer:
        to = row.get("to") or row.get("contractAddress") or ""
        return Transfer(
            chain=chain,
            tx_hash=row["hash"],
            block_number=int(row["blockNumber"]),
            timestamp=_ts(row["timeStamp"]),
            from_address=row["from"].lower(),
            to_address=to.lower(),
            asset="ETH",
            amount=_scaled(row["value"], 18),
        )

    @staticmethod
    def _token(chain: Chain, row: dict[str, Any]) -> Transfer:
        return Transfer(
            chain=chain,
            tx_hash=row["hash"],
            block_number=int(row["blockNumber"]),
            timestamp=_ts(row["timeStamp"]),
            from_address=row["from"].lower(),
            to_address=row["to"].lower(),
            asset=row.get("tokenSymbol") or row["contractAddress"].lower(),
            amount=_scaled(row["value"], int(row.get("tokenDecimal") or 0)),
        )

    async def _contract_label(self, chain: Chain, address: str) -> str:
        result = await self._call(chain, module="contract", action="getsourcecode", address=address)
        name = result[0].get("ContractName", "") if isinstance(result, list) and result else ""
        return f"contract:{name}" if name else "contract:unverified"

    async def _records(
        self, chain: Chain, action: str, address: str, *, limit: int, newest_first: bool = True
    ) -> list[dict[str, Any]]:
        result = await self._call(
            chain,
            module="account",
            action=action,
            address=address,
            startblock=0,
            endblock=99_999_999,
            page=1,
            offset=limit,
            sort="desc" if newest_first else "asc",
        )
        return list(result) if isinstance(result, list) else []

    async def _proxy(self, chain: Chain, action: str, **params: Any) -> str:
        result = await self._call(chain, module="proxy", action=action, **params)
        return str(result)

    async def _call(self, chain: Chain, **params: Any) -> Any:
        if chain not in CHAIN_IDS:
            raise EtherscanError(f"unsupported chain: {chain}")
        query = {"chainid": CHAIN_IDS[chain], **params, "apikey": self._key}

        async def send() -> httpx.Response:
            await self._limiter.wait()
            return await self._client.get(self._base_url, params=query)

        try:
            response = await request_with_retry(
                send,
                max_retries=self._max_retries,
                backoff_base=self._backoff_base,
                check=self._check_body,
            )
        except httpx.HTTPStatusError as exc:
            raise EtherscanError(
                f"Etherscan HTTP {exc.response.status_code} for {params.get('action')}"
            ) from exc
        except httpx.TransportError as exc:
            raise EtherscanError(f"Etherscan unreachable: {exc!r}") from exc
        except RetryableError as exc:
            raise EtherscanError(f"Etherscan rate limit persisted: {exc}") from exc
        return response.json()["result"]

    @staticmethod
    def _check_body(response: httpx.Response) -> None:
        try:
            body = response.json()
        except ValueError as exc:
            raise EtherscanError("Etherscan returned a non-JSON body") from exc
        if not isinstance(body, dict) or "result" not in body:
            raise EtherscanError(f"unexpected Etherscan response: {str(body)[:200]}")

        if "jsonrpc" in body:  # proxy module: JSON-RPC shape
            error = body.get("error")
            if error:
                message = str(error.get("message", error) if isinstance(error, dict) else error)
                if _is_rate_limited(message):
                    raise RetryableError(message)
                raise EtherscanError(f"Etherscan proxy error: {message}")
            result = body["result"]
            if isinstance(result, str) and _is_rate_limited(result):
                raise RetryableError(result)
            return

        if body.get("status") == "1":
            return
        message = str(body.get("message", ""))
        result_text = body["result"] if isinstance(body["result"], str) else ""
        if message.lower().startswith(_NO_RECORDS):
            return
        if _is_rate_limited(result_text) or _is_rate_limited(message):
            raise RetryableError(result_text or message)
        raise EtherscanError(f"Etherscan error: {message}: {result_text}".rstrip(": "))
