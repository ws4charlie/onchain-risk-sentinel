import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from sentinel.core.config import Settings
from sentinel.core.ports import ChainDataSource
from sentinel.core.schemas import Chain
from sentinel.tools.etherscan import BASE_URL, EtherscanChainData, EtherscanError

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "etherscan_responses.json").read_text())
ADDRESS = "0x" + "ab" * 20


def _route(overrides: dict[str, str] | None = None) -> Any:
    """Answer each Etherscan call with the fixture matching its module/action/sort."""
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        assert params["chainid"] == "1"
        assert params["apikey"] == "test-key"
        action, sort = params["action"], params.get("sort")
        key = {
            "balance": "balance",
            "eth_getCode": "code_eoa",
            "eth_getTransactionCount": "nonce",
            "getsourcecode": "sourcecode_verified",
            "txlistinternal": "empty",
        }.get(action)
        if action in ("txlist", "tokentx"):
            key = f"{action}_desc" if sort == "desc" else f"{action}_asc_first"
        key = overrides.get(action, key)
        assert key is not None, f"unexpected action {action}"
        return httpx.Response(200, json=FIXTURES[key])

    return handler


def _client(**kwargs: Any) -> EtherscanChainData:
    return EtherscanChainData(
        "test-key", rate_per_second=1000, backoff_base=0, max_retries=2, **kwargs
    )


async def test_satisfies_port() -> None:
    async with _client() as source:
        assert isinstance(source, ChainDataSource)


@respx.mock
async def test_address_profile_for_eoa() -> None:
    respx.get(BASE_URL).mock(side_effect=_route())
    async with _client() as source:
        profile = await source.get_address_profile(
            Chain.ETHEREUM, ADDRESS.upper().replace("0X", "0x")
        )

    assert profile.address == ADDRESS
    assert profile.is_contract is False
    assert profile.balance_wei == 1_500_000_000_000_000_000
    assert profile.tx_count == 26
    assert profile.first_seen == datetime.fromtimestamp(1656633600, tz=UTC)
    assert profile.last_seen == datetime.fromtimestamp(1759310000, tz=UTC)
    assert profile.labels == []


@respx.mock
async def test_address_profile_for_contract_has_name_label() -> None:
    respx.get(BASE_URL).mock(side_effect=_route({"eth_getCode": "code_contract"}))
    async with _client() as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.is_contract is True
    assert profile.labels == ["contract:Vault"]


@respx.mock
async def test_fresh_address_has_no_dates() -> None:
    respx.get(BASE_URL).mock(
        side_effect=_route(
            {"txlist": "empty", "tokentx": "empty", "eth_getTransactionCount": "nonce_zero"}
        )
    )
    async with _client() as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.first_seen is None and profile.last_seen is None
    assert profile.tx_count == 0


@respx.mock
async def test_recent_transfers_merge_sort_and_skip_failed() -> None:
    respx.get(BASE_URL).mock(side_effect=_route())
    async with _client() as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=10)

    assert [t.block_number for t in transfers] == [21000060, 21000050, 21000030]
    zero_usdt, eth_out, usdt_in = transfers
    assert zero_usdt.asset == "USDT" and zero_usdt.amount == 0
    assert zero_usdt.from_address == "0x1111999999999999999999999999999999991a2b"
    assert eth_out.asset == "ETH" and eth_out.amount == pytest.approx(0.25)
    assert eth_out.from_address == ADDRESS  # lowercased from the checksummed fixture
    assert usdt_in.amount == pytest.approx(1250.5)


@respx.mock
async def test_recent_transfers_respects_limit() -> None:
    respx.get(BASE_URL).mock(side_effect=_route())
    async with _client() as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=1)
    assert len(transfers) == 1


@respx.mock
async def test_retries_on_http_429_then_succeeds() -> None:
    route = respx.get(BASE_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=FIXTURES["balance"]),
            httpx.Response(200, json=FIXTURES["code_eoa"]),
            httpx.Response(200, json=FIXTURES["nonce"]),
            *[httpx.Response(200, json=FIXTURES["empty"])] * 3,
        ]
    )
    async with _client() as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.balance_wei == 1_500_000_000_000_000_000
    assert route.call_count == 7


@respx.mock
async def test_retries_on_in_body_rate_limit() -> None:
    route = respx.get(BASE_URL).mock(
        side_effect=[
            httpx.Response(200, json=FIXTURES["rate_limited"]),
            httpx.Response(200, json=FIXTURES["txlist_desc"]),
            httpx.Response(200, json=FIXTURES["empty"]),
            httpx.Response(200, json=FIXTURES["empty"]),
        ]
    )
    async with _client() as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)
    assert len(transfers) == 1
    assert route.call_count == 4


@respx.mock
async def test_gives_up_after_max_retries() -> None:
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(503))
    async with _client() as source:
        with pytest.raises(EtherscanError, match="HTTP 503"):
            await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)
    assert route.call_count == 3


@respx.mock
async def test_invalid_key_is_not_retried() -> None:
    route = respx.get(BASE_URL).mock(return_value=httpx.Response(200, json=FIXTURES["invalid_key"]))
    async with _client() as source:
        with pytest.raises(EtherscanError, match="Invalid API Key"):
            await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert route.call_count == 1


@respx.mock
async def test_transport_error_becomes_etherscan_error() -> None:
    respx.get(BASE_URL).mock(side_effect=httpx.ConnectError("boom"))
    async with _client() as source:
        with pytest.raises(EtherscanError, match="unreachable"):
            await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)


async def test_rejects_bad_address() -> None:
    async with _client() as source:
        with pytest.raises(ValueError):
            await source.get_address_profile(Chain.ETHEREUM, "0x123")


def test_requires_api_key() -> None:
    with pytest.raises(EtherscanError):
        EtherscanChainData("")
    with pytest.raises(EtherscanError, match="SENTINEL_ETHERSCAN_API_KEY"):
        EtherscanChainData.from_settings(Settings(etherscan_api_key=None))


@pytest.mark.live
async def test_live_etherscan() -> None:
    settings = Settings()
    if settings.etherscan_api_key is None and not os.environ.get("SENTINEL_ETHERSCAN_API_KEY"):
        pytest.skip("SENTINEL_ETHERSCAN_API_KEY not set")
    # A long-lived, well-known address (the Ethereum Foundation's original multisig).
    address = "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae"
    async with EtherscanChainData.from_settings(settings) as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, address)
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, address, limit=5)
    assert profile.first_seen is not None
    assert profile.is_contract is True
    assert len(transfers) <= 5
