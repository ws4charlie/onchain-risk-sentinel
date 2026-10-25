"""`ChainDataSource` that tries several sources in order.

Typical setup (see `FallbackChainData.from_settings`): `RpcChainData` first, then
`EtherscanChainData`, which also supplies contract names the node providers lack.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self, TypeVar

from sentinel.core.config import Settings
from sentinel.core.ports import ChainDataSource
from sentinel.core.schemas import AddressProfile, Chain, Transfer
from sentinel.tools.etherscan import EtherscanChainData
from sentinel.tools.rpc import RpcChainData, RpcSettings

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ChainDataConfigError(RuntimeError):
    """No chain-data source is configured."""


class ChainDataUnavailableError(RuntimeError):
    """Every chain-data source failed. `failures` holds (source name, error) pairs."""

    def __init__(self, method: str, failures: list[tuple[str, Exception]]) -> None:
        self.failures = failures
        detail = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in failures)
        super().__init__(f"all chain-data sources failed for {method}: {detail}")


def _name(source: object) -> str:
    return type(source).__name__


def _has_name(labels: list[str]) -> bool:
    return any(label.startswith("contract:") for label in labels)


class FallbackChainData:
    """Try `primary`, then each fallback, per call; raise one error if all fail.

    `enrich_labels_from`, if given, names contracts whose profile lacks a
    `contract:<name>` label (best effort; never fails the call). A source with a
    `contract_label(chain, address) -> str | None` method is asked for just that;
    any other `ChainDataSource` is asked for its full profile.
    """

    def __init__(
        self,
        primary: ChainDataSource,
        *fallbacks: ChainDataSource,
        enrich_labels_from: ChainDataSource | None = None,
    ) -> None:
        self._sources: tuple[ChainDataSource, ...] = (primary, *fallbacks)
        self._enricher = enrich_labels_from

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        rpc_settings: RpcSettings | None = None,
        **kwargs: Any,
    ) -> Self:
        """RPC first and Etherscan second (also naming contracts), from whichever is set.

        `kwargs` are passed to every adapter constructor (e.g. a shared `client`).
        """
        settings = settings or Settings()
        rpc_settings = rpc_settings or RpcSettings()
        sources: list[ChainDataSource] = []
        if rpc_settings.eth_rpc_url is not None or rpc_settings.alchemy_api_key is not None:
            sources.append(RpcChainData.from_settings(rpc_settings, **kwargs))
        etherscan = (
            EtherscanChainData.from_settings(settings, **kwargs)
            if settings.etherscan_api_key is not None
            else None
        )
        if etherscan is not None:
            sources.append(etherscan)
        if not sources:
            raise ChainDataConfigError(
                "no chain-data source configured: set SENTINEL_ETH_RPC_URL or "
                "SENTINEL_ALCHEMY_API_KEY, and/or SENTINEL_ETHERSCAN_API_KEY"
            )
        return cls(*sources, enrich_labels_from=etherscan)

    async def aclose(self) -> None:
        closed: set[int] = set()
        for source in (*self._sources, self._enricher):
            close = getattr(source, "aclose", None)
            if close is not None and id(source) not in closed:
                closed.add(id(source))
                await close()

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
        profile = await self._first_success(
            "get_address_profile", lambda s: s.get_address_profile(chain, address)
        )
        if profile.is_contract and not _has_name(profile.labels):
            profile = await self._enrich(profile)
        return profile

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        return await self._first_success(
            "get_recent_transfers", lambda s: s.get_recent_transfers(chain, address, limit)
        )

    # -- helpers -----------------------------------------------------------------------

    async def _first_success(
        self, method: str, call: Callable[[ChainDataSource], Awaitable[T]]
    ) -> T:
        failures: list[tuple[str, Exception]] = []
        for source in self._sources:
            try:
                return await call(source)
            except Exception as exc:  # any source failure moves on to the next source
                failures.append((_name(source), exc))
                logger.warning(
                    "%s.%s failed (%s: %s); trying next source",
                    _name(source),
                    method,
                    type(exc).__name__,
                    exc,
                )
        raise ChainDataUnavailableError(method, failures)

    async def _enrich(self, profile: AddressProfile) -> AddressProfile:
        if self._enricher is None:
            return profile
        try:
            label = await self._contract_label(self._enricher, profile)
        except Exception as exc:  # enrichment is best effort
            logger.warning(
                "contract-name lookup via %s failed (%s: %s)",
                _name(self._enricher),
                type(exc).__name__,
                exc,
            )
            return profile
        if not label:
            return profile
        labels = [label if item == "contract" else item for item in profile.labels]
        if label not in labels:
            labels.append(label)
        return profile.model_copy(update={"labels": labels})

    @staticmethod
    async def _contract_label(source: ChainDataSource, profile: AddressProfile) -> str | None:
        lookup = getattr(source, "contract_label", None)
        if callable(lookup):
            label = await lookup(profile.chain, profile.address)
            return str(label) if label else None
        named = await source.get_address_profile(profile.chain, profile.address)
        return next((label for label in named.labels if label.startswith("contract:")), None)
