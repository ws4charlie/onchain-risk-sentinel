import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from sentinel.core.ports import ChainDataSource
from sentinel.core.schemas import Chain
from sentinel.tools.rpc import (
    ALCHEMY_URLS,
    HistoryNotConfiguredError,
    JsonRpcClient,
    RpcChainData,
    RpcError,
    RpcSettings,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "alchemy_transfers.json").read_text())
ADDRESS = "0x" + "ab" * 20
RPC_KEY = "rpc-secret"
RPC_URL = f"https://rpc.example.test/v1/{RPC_KEY}"
ALCHEMY_KEY = "alchemy-secret"
ALCHEMY_URL = ALCHEMY_URLS[Chain.ETHEREUM] + ALCHEMY_KEY

BASICS = {
    "eth_getBalance": "0x14d1120d7b160000",  # 1.5 ETH
    "eth_getCode": "0x",
    "eth_getTransactionCount": "0x1a",
}


def _rpc_ok(request: httpx.Request, result: Any) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _rpc_error(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}}
    )


def _route(
    basics: dict[str, str] | None = None,
    transfers: dict[str, str] | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> Any:
    """Answer each JSON-RPC call; transfers by '<direction>_<order>' fixture key."""
    basics = {**BASICS, **(basics or {})}
    keys = {"from_desc": "from_desc", "to_desc": "to_desc"}
    keys.update({"from_asc": "from_asc_first", "to_asc": "to_asc_first"})
    keys.update(transfers or {})

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["jsonrpc"] == "2.0"
        if calls is not None:
            calls.append({"url": str(request.url), **body})
        method = body["method"]
        if method in basics:
            assert body["params"] == [ADDRESS, "latest"]
            return _rpc_ok(request, basics[method])
        assert method == "alchemy_getAssetTransfers", method
        (params,) = body["params"]
        direction = "from" if "fromAddress" in params else "to"
        assert params[f"{direction}Address"] == ADDRESS
        return _rpc_ok(request, FIXTURES[keys[f"{direction}_{params['order']}"]])

    return handler


def _source(**kwargs: Any) -> RpcChainData:
    options: dict[str, Any] = {"rate_per_second": 1000, "backoff_base": 0, "max_retries": 2}
    options.update(kwargs)
    return RpcChainData(**options)


async def test_satisfies_port() -> None:
    async with _source(rpc_url=RPC_URL) as source:
        assert isinstance(source, ChainDataSource)


@respx.mock
async def test_address_profile_for_eoa() -> None:
    calls: list[dict[str, Any]] = []
    respx.post(RPC_URL).mock(side_effect=_route(calls=calls))
    respx.post(ALCHEMY_URL).mock(side_effect=_route(calls=calls))
    async with _source(rpc_url=RPC_URL, alchemy_api_key=ALCHEMY_KEY) as source:
        profile = await source.get_address_profile(
            Chain.ETHEREUM, ADDRESS.upper().replace("0X", "0x")
        )

    assert profile.address == ADDRESS
    assert profile.is_contract is False
    assert profile.balance_wei == 1_500_000_000_000_000_000
    assert profile.tx_count == 26
    assert profile.first_seen == datetime(2022, 7, 1, tzinfo=UTC)  # oldest inbound
    assert profile.last_seen == datetime(2025, 10, 1, 10, tzinfo=UTC)  # newest inbound
    assert profile.labels == []

    basics = [c for c in calls if c["method"] != "alchemy_getAssetTransfers"]
    assert {c["url"] for c in basics} == {RPC_URL}
    assert sorted(c["method"] for c in basics) == sorted(BASICS)

    history = [c for c in calls if c["method"] == "alchemy_getAssetTransfers"]
    assert {c["url"] for c in history} == {ALCHEMY_URL}
    shapes = sorted(("fromAddress" in c["params"][0], c["params"][0]["order"]) for c in history)
    assert shapes == [(False, "asc"), (False, "desc"), (True, "asc"), (True, "desc")]
    for call in history:
        params = call["params"][0]
        assert params["category"] == ["external", "internal", "erc20"]
        assert params["maxCount"] == "0x1"
        assert params["withMetadata"] is True
        assert params["excludeZeroValue"] is False
        assert params["fromBlock"] == "0x0" and params["toBlock"] == "latest"
        assert "pageKey" not in params  # one record is enough; never follow the page key


@respx.mock
async def test_address_profile_for_contract_is_labelled_without_name() -> None:
    respx.post(ALCHEMY_URL).mock(side_effect=_route({"eth_getCode": "0x6080604052"}))
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.is_contract is True
    assert profile.labels == ["contract"]


@respx.mock
async def test_alchemy_key_alone_serves_json_rpc_too() -> None:
    route = respx.post(ALCHEMY_URL).mock(side_effect=_route())
    async with _source(alchemy_api_key=SecretStr(ALCHEMY_KEY)) as source:
        await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert route.call_count == 3 + 4


@respx.mock
async def test_fresh_address_has_no_dates() -> None:
    empty = {k: "empty" for k in ("from_asc", "from_desc", "to_asc", "to_desc")}
    respx.post(ALCHEMY_URL).mock(
        side_effect=_route({"eth_getTransactionCount": "0x0", "eth_getBalance": "0x0"}, empty)
    )
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
    assert profile.first_seen is None and profile.last_seen is None
    assert profile.tx_count == 0 and profile.balance_wei == 0


@respx.mock
async def test_recent_transfers_merge_scale_and_keep_zero_value() -> None:
    respx.post(ALCHEMY_URL).mock(side_effect=_route())
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=10)

    assert [t.block_number for t in transfers] == [
        21000072,
        21000060,
        21000032,
        21000030,
        21000016,
    ]
    zero_usdt, eth_out, internal_out, usdt_in, unknown_token = transfers
    assert zero_usdt.asset == "USDT" and zero_usdt.amount == 0
    assert zero_usdt.from_address == "0x1111999999999999999999999999999999991a2b"
    assert zero_usdt.timestamp == datetime(2025, 10, 1, 10, tzinfo=UTC)
    assert eth_out.asset == "ETH" and eth_out.amount == pytest.approx(0.25)
    assert eth_out.from_address == ADDRESS
    assert internal_out.asset == "ETH" and internal_out.amount == pytest.approx(1.0)
    assert usdt_in.amount == pytest.approx(1250.5)
    # Unknown symbol and decimals: contract address as asset, raw units as amount.
    assert unknown_token.asset == "0xc0ffee0000000000000000000000000000000001"
    assert unknown_token.amount == 123


@respx.mock
async def test_recent_transfers_request_shape() -> None:
    calls: list[dict[str, Any]] = []
    respx.post(ALCHEMY_URL).mock(side_effect=_route(calls=calls))
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=5000)
    assert len(calls) == 2
    for call in calls:
        params = call["params"][0]
        assert params["order"] == "desc"
        assert params["maxCount"] == "0x3e8"  # capped at Alchemy's page size
        assert params["excludeZeroValue"] is False


@respx.mock
async def test_recent_transfers_respects_limit() -> None:
    respx.post(ALCHEMY_URL).mock(side_effect=_route())
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=2)
        none = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=0)
    assert [t.block_number for t in transfers] == [21000072, 21000060]
    assert none == []


@respx.mock
async def test_self_transfer_is_not_duplicated() -> None:
    row = FIXTURES["from_desc"]["transfers"][0]
    self_row = {**row, "to": ADDRESS}
    page = {"transfers": [self_row], "pageKey": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        return _rpc_ok(request, page)

    respx.post(ALCHEMY_URL).mock(side_effect=handler)
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)
    assert len(transfers) == 1


@respx.mock
async def test_pagination_follows_page_key_until_limit() -> None:
    rows = FIXTURES["to_desc"]["transfers"]
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        (params,) = json.loads(request.content)["params"]
        if "fromAddress" in params:
            return _rpc_ok(request, FIXTURES["empty"])
        seen.append(params)
        if "pageKey" not in params:
            return _rpc_ok(request, {"transfers": rows[:2], "pageKey": "page-2"})
        assert params["pageKey"] == "page-2"
        return _rpc_ok(request, {"transfers": rows[2:], "pageKey": "page-3"})

    respx.post(ALCHEMY_URL).mock(side_effect=handler)
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS, limit=3)
    assert len(transfers) == 3
    assert [p["maxCount"] for p in seen] == ["0x3", "0x1"]  # asks only for what is missing


@respx.mock
async def test_retries_on_http_429_then_succeeds() -> None:
    responses = iter([httpx.Response(429, headers={"Retry-After": "0"})])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, None) or _rpc_ok(request, "0x10")

    route = respx.post(RPC_URL).mock(side_effect=handler)
    async with JsonRpcClient(RPC_URL, rate_per_second=1000, backoff_base=0) as rpc:
        assert await rpc.call("eth_getBalance", [ADDRESS, "latest"]) == "0x10"
    assert route.call_count == 2


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (429, "Your app has exceeded its compute units per second capacity"),
        (-32005, "limit exceeded"),
        (-32000, "rate limit reached, try again later"),
    ],
)
@respx.mock
async def test_retries_on_in_body_rate_limit(code: int, message: str) -> None:
    responses = iter([_rpc_error(code, message)])

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, None) or _rpc_ok(request, "0x10")

    route = respx.post(RPC_URL).mock(side_effect=handler)
    async with JsonRpcClient(RPC_URL, rate_per_second=1000, backoff_base=0) as rpc:
        assert await rpc.call("eth_getBalance", [ADDRESS, "latest"]) == "0x10"
    assert route.call_count == 2


@respx.mock
async def test_gives_up_after_max_retries_without_leaking_url() -> None:
    route = respx.post(RPC_URL).mock(return_value=httpx.Response(503))
    async with JsonRpcClient(RPC_URL, rate_per_second=1000, backoff_base=0, max_retries=2) as rpc:
        with pytest.raises(RpcError, match="HTTP 503 for eth_getBalance") as info:
            await rpc.call("eth_getBalance", [ADDRESS, "latest"])
    assert route.call_count == 3
    assert RPC_KEY not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


@respx.mock
async def test_json_rpc_error_is_not_retried() -> None:
    route = respx.post(RPC_URL).mock(return_value=_rpc_error(-32602, "invalid argument 0"))
    async with JsonRpcClient(RPC_URL, rate_per_second=1000, backoff_base=0) as rpc:
        with pytest.raises(RpcError, match=r"-32602: invalid argument 0 \(method eth_getCode\)"):
            await rpc.call("eth_getCode", ["0x0", "latest"])
    assert route.call_count == 1


@respx.mock
async def test_transport_error_becomes_rpc_error() -> None:
    respx.post(RPC_URL).mock(side_effect=httpx.ConnectError(f"cannot reach {RPC_URL}"))
    async with JsonRpcClient(
        RPC_URL, name="node", rate_per_second=1000, backoff_base=0, max_retries=1
    ) as rpc:
        with pytest.raises(RpcError, match="node unreachable") as info:
            await rpc.call("eth_getBalance", [ADDRESS, "latest"])
    assert RPC_KEY not in str(info.value)


@respx.mock
async def test_non_json_body_is_an_error() -> None:
    respx.post(RPC_URL).mock(return_value=httpx.Response(200, text="<html>"))
    async with JsonRpcClient(RPC_URL, rate_per_second=1000) as rpc:
        with pytest.raises(RpcError, match="non-JSON"):
            await rpc.call("eth_getBalance", [ADDRESS, "latest"])


@respx.mock
async def test_missing_history_key_raises_before_any_call() -> None:
    route = respx.post(RPC_URL).mock(side_effect=_route())
    async with _source(rpc_url=RPC_URL) as source:
        with pytest.raises(HistoryNotConfiguredError, match="history provider not configured"):
            await source.get_address_profile(Chain.ETHEREUM, ADDRESS)
        with pytest.raises(HistoryNotConfiguredError):
            await source.get_recent_transfers(Chain.ETHEREUM, ADDRESS)
    assert route.call_count == 0


async def test_rejects_bad_address() -> None:
    async with _source(alchemy_api_key=ALCHEMY_KEY) as source:
        with pytest.raises(ValueError):
            await source.get_address_profile(Chain.ETHEREUM, "0x123")


def test_requires_a_provider() -> None:
    with pytest.raises(RpcError):
        RpcChainData()
    with pytest.raises(RpcError, match="SENTINEL_ETH_RPC_URLS"):
        RpcChainData.from_settings(RpcSettings(eth_rpc_urls=None, alchemy_api_key=None))
    with pytest.raises(RpcError):
        JsonRpcClient("")


def test_from_settings_reads_secrets() -> None:
    settings = RpcSettings(eth_rpc_urls=SecretStr(RPC_URL), alchemy_api_key=None)
    source = RpcChainData.from_settings(settings)
    assert isinstance(source, RpcChainData)
    assert RPC_KEY not in repr(settings)


# A long-lived, well-known address (the Ethereum Foundation's original multisig).
LIVE_ADDRESS = "0xde0b295669a9fd93d5f28d9ec85e40f4cb697bae"


@pytest.mark.live
async def test_live_json_rpc() -> None:
    urls = RpcSettings().rpc_urls()
    if not urls:
        pytest.skip("SENTINEL_ETH_RPC_URLS not set")
    url = urls[0]
    async with JsonRpcClient(url) as rpc:
        code = await rpc.call("eth_getCode", [LIVE_ADDRESS, "latest"])
        balance = await rpc.call("eth_getBalance", [LIVE_ADDRESS, "latest"])
    assert code not in ("0x", "")
    assert int(balance, 16) >= 0


@pytest.mark.live
async def test_live_alchemy() -> None:
    settings = RpcSettings()
    if settings.alchemy_api_key is None:
        pytest.skip("SENTINEL_ALCHEMY_API_KEY not set")
    async with RpcChainData(alchemy_api_key=settings.alchemy_api_key) as source:
        profile = await source.get_address_profile(Chain.ETHEREUM, LIVE_ADDRESS)
        transfers = await source.get_recent_transfers(Chain.ETHEREUM, LIVE_ADDRESS, limit=5)
    assert profile.is_contract is True
    assert profile.first_seen is not None and profile.first_seen.year == 2015
    assert profile.last_seen is not None and profile.last_seen >= profile.first_seen
    assert 0 < len(transfers) <= 5
