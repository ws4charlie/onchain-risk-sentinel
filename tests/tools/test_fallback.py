import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import pytest

from sentinel.core.config import Settings
from sentinel.core.ports import ChainDataSource
from sentinel.core.schemas import AddressProfile, Chain, Transfer
from sentinel.tools.etherscan import EtherscanChainData
from sentinel.tools.fallback import (
    ChainDataConfigError,
    ChainDataUnavailableError,
    FallbackChainData,
)
from sentinel.tools.rpc import HistoryNotConfiguredError, RpcChainData, RpcSettings

ADDRESS = "0x" + "ab" * 20


def _profile(*, is_contract: bool = False, labels: list[str] | None = None) -> AddressProfile:
    return AddressProfile(
        chain=Chain.ETHEREUM,
        address=ADDRESS,
        is_contract=is_contract,
        balance_wei=1,
        tx_count=2,
        labels=labels or [],
    )


def _transfer(block: int) -> Transfer:
    return Transfer(
        chain=Chain.ETHEREUM,
        tx_hash=f"0x{block:064x}",
        block_number=block,
        timestamp=datetime(2025, 1, 1, tzinfo=UTC),
        from_address=ADDRESS,
        to_address="0x" + "cd" * 20,
        asset="ETH",
        amount=1.0,
    )


class StubSource:
    """Records calls; returns fixed results or raises `error`."""

    def __init__(
        self,
        *,
        profile: AddressProfile | None = None,
        transfers: list[Transfer] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.profile = profile or _profile()
        self.transfers = transfers or []
        self.error = error
        self.calls: list[str] = []
        self.closed = False

    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        self.calls.append("profile")
        if self.error:
            raise self.error
        return self.profile

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        self.calls.append(f"transfers:{limit}")
        if self.error:
            raise self.error
        return self.transfers[:limit]

    async def aclose(self) -> None:
        self.closed = True


class LabelSource(StubSource):
    def __init__(self, label: str | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.label = label

    async def contract_label(self, chain: Chain, address: str) -> str | None:
        self.calls.append("label")
        if self.error:
            raise self.error
        return self.label


def test_satisfies_port() -> None:
    assert isinstance(FallbackChainData(StubSource()), ChainDataSource)


async def test_uses_primary_when_it_works() -> None:
    primary, backup = StubSource(transfers=[_transfer(1)]), StubSource()
    source = FallbackChainData(primary, backup)
    assert await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=7) == [_transfer(1)]
    assert primary.calls == ["transfers:7"] and backup.calls == []


async def test_falls_back_in_order_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    first = StubSource(error=HistoryNotConfiguredError("history provider not configured"))
    second = StubSource(error=RuntimeError("down"))
    third = StubSource(profile=_profile(labels=["x"]))
    source = FallbackChainData(first, second, third)
    with caplog.at_level(logging.WARNING, logger="sentinel.tools.fallback"):
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.labels == ["x"]
    assert first.calls == second.calls == third.calls == ["profile"]
    assert len(caplog.records) == 2
    assert "HistoryNotConfiguredError" in caplog.records[0].getMessage()
    assert "trying next source" in caplog.records[1].getMessage()


async def test_all_fail_raises_one_error_naming_every_source() -> None:
    source = FallbackChainData(
        StubSource(error=RuntimeError("rpc down")), LabelSource(None, error=ValueError("bad key"))
    )
    with pytest.raises(ChainDataUnavailableError) as info:
        await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)
    message = str(info.value)
    assert "get_recent_transfers" in message
    assert "StubSource: RuntimeError: rpc down" in message
    assert "LabelSource: ValueError: bad key" in message
    assert [name for name, _ in info.value.failures] == ["StubSource", "LabelSource"]


async def test_cancellation_is_not_swallowed() -> None:
    backup = StubSource()
    source = FallbackChainData(StubSource(error=asyncio.CancelledError()), backup)
    with pytest.raises(asyncio.CancelledError):
        await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert backup.calls == []


async def test_enriches_contract_name_via_contract_label() -> None:
    primary = StubSource(profile=_profile(is_contract=True, labels=["contract"]))
    names = LabelSource("contract:Vault")
    source = FallbackChainData(primary, enrich_labels_from=names)
    profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.labels == ["contract:Vault"]
    assert names.calls == ["label"]


async def test_enriches_contract_name_via_full_profile() -> None:
    primary = StubSource(profile=_profile(is_contract=True, labels=["contract"]))
    names = StubSource(profile=_profile(is_contract=True, labels=["contract:Router"]))
    source = FallbackChainData(primary, enrich_labels_from=names)
    profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.labels == ["contract:Router"]


async def test_enrichment_skipped_for_eoa_and_named_contracts() -> None:
    names = LabelSource("contract:Vault")
    eoa = FallbackChainData(StubSource(), enrich_labels_from=names)
    named = FallbackChainData(
        StubSource(profile=_profile(is_contract=True, labels=["contract:Token"])),
        enrich_labels_from=names,
    )
    assert (await eoa.get_address_profile(Chain.ETHEREUM, ADDRESS)).labels == []
    assert (await named.get_address_profile(Chain.ETHEREUM, ADDRESS)).labels == ["contract:Token"]
    assert names.calls == []


async def test_enrichment_failure_never_fails_the_call(caplog: pytest.LogCaptureFixture) -> None:
    primary = StubSource(profile=_profile(is_contract=True, labels=["contract"]))
    names = LabelSource(None, error=RuntimeError("etherscan down"))
    source = FallbackChainData(primary, enrich_labels_from=names)
    with caplog.at_level(logging.WARNING, logger="sentinel.tools.fallback"):
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.labels == ["contract"]
    assert "contract-name lookup" in caplog.text


async def test_enrichment_without_a_name_keeps_labels() -> None:
    primary = StubSource(profile=_profile(is_contract=True, labels=["contract"]))
    source = FallbackChainData(primary, enrich_labels_from=LabelSource(None))
    assert (await source.get_address_profile(Chain.ETHEREUM, ADDRESS)).labels == ["contract"]


async def test_aclose_closes_each_source_once() -> None:
    primary, backup = StubSource(), StubSource()
    async with FallbackChainData(primary, backup, enrich_labels_from=backup):
        pass
    assert primary.closed and backup.closed


async def test_from_settings_builds_rpc_then_etherscan() -> None:
    source = FallbackChainData.from_settings(
        Settings(etherscan_api_key="e-key"),
        RpcSettings(eth_rpc_urls="https://rpc.example.test/k", alchemy_api_key="a-key"),
    )
    async with source:
        assert [type(s) for s in source._sources] == [RpcChainData, EtherscanChainData]
        assert isinstance(source._enricher, EtherscanChainData)


async def test_from_settings_with_one_source() -> None:
    rpc_only = FallbackChainData.from_settings(
        Settings(etherscan_api_key=None), RpcSettings(eth_rpc_urls=None, alchemy_api_key="a-key")
    )
    async with rpc_only:
        assert [type(s) for s in rpc_only._sources] == [RpcChainData]
        assert rpc_only._enricher is None
    etherscan_only = FallbackChainData.from_settings(
        Settings(etherscan_api_key="e-key"), RpcSettings(eth_rpc_urls=None, alchemy_api_key=None)
    )
    async with etherscan_only:
        assert [type(s) for s in etherscan_only._sources] == [EtherscanChainData]


async def test_from_settings_builds_one_rpc_source_per_url() -> None:
    source = FallbackChainData.from_settings(
        Settings(etherscan_api_key="e-key"),
        RpcSettings(
            eth_rpc_urls="https://a.example.test/k1, https://b.example.test/k2,",
            alchemy_api_key="a-key",
        ),
    )
    async with source:
        assert [type(s) for s in source._sources] == [
            RpcChainData,
            RpcChainData,
            EtherscanChainData,
        ]


def test_from_settings_requires_a_source() -> None:
    with pytest.raises(ChainDataConfigError, match="no chain-data source configured"):
        FallbackChainData.from_settings(
            Settings(etherscan_api_key=None), RpcSettings(eth_rpc_urls=None, alchemy_api_key=None)
        )
