import json

import pytest
from langchain_core.tools import BaseTool

from sentinel.core.schemas import AddressProfile, Chain, PatternDoc, Transfer
from sentinel.tools.langchain_tools import (
    chain_data_tools,
    pattern_tools,
    sanctions_tools,
    scam_intel_tools,
    tools_by_specialist,
)
from tests.fakes import (
    CLEAN,
    DRAINER,
    SANCTIONED,
    FakeChainData,
    FakePatterns,
    FakeSanctions,
    FakeScamIntel,
    fake_ports,
)


def _by_name(tools: list[BaseTool]) -> dict[str, BaseTool]:
    return {t.name: t for t in tools}


class BrokenChainData(FakeChainData):
    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        raise RuntimeError("upstream down")


async def test_tools_by_specialist_names() -> None:
    groups = tools_by_specialist(fake_ports())
    assert {k: sorted(t.name for t in v) for k, v in groups.items()} == {
        "chain_tracer": ["get_address_profile", "get_recent_transfers"],
        "sanctions": ["check_sanctions"],
        "scam_intel": ["lookup_scam_reports"],
        "pattern_analyst": ["search_fraud_patterns"],
    }
    for tools in groups.values():
        for tool in tools:
            assert len(tool.description) > 40
            assert tool.args_schema is not None


async def test_address_profile_tool_normalizes_and_serializes() -> None:
    tool = _by_name(chain_data_tools(FakeChainData()))["get_address_profile"]
    out = await tool.ainvoke({"address": CLEAN.upper().replace("0X", "0x")})
    profile = AddressProfile.model_validate_json(out)
    assert profile.address == CLEAN
    assert profile.tx_count == 412


async def test_transfers_tool_respects_limit() -> None:
    tool = _by_name(chain_data_tools(FakeChainData()))["get_recent_transfers"]
    rows = json.loads(await tool.ainvoke({"address": CLEAN, "limit": 1}))
    assert len(rows) == 1
    Transfer.model_validate(rows[0])


async def test_sanctions_tool_hit_and_miss() -> None:
    tool = sanctions_tools(FakeSanctions())[0]
    hit = json.loads(await tool.ainvoke({"address": SANCTIONED}))
    assert hit["list_name"] == "OFAC SDN"
    assert json.loads(await tool.ainvoke({"address": CLEAN})) is None


async def test_scam_tool_returns_reports() -> None:
    tool = scam_intel_tools(FakeScamIntel())[0]
    reports = json.loads(await tool.ainvoke({"address": DRAINER}))
    assert reports[0]["category"] == "drainer"


async def test_pattern_tool_returns_docs() -> None:
    tool = pattern_tools(FakePatterns())[0]
    docs = json.loads(await tool.ainvoke({"query": "zero-value transfers look-alike", "k": 2}))
    assert len(docs) == 2
    assert PatternDoc.model_validate(docs[0]).doc_id == "address-poisoning"


async def test_bad_address_is_reported_to_the_model() -> None:
    tool = sanctions_tools(FakeSanctions())[0]
    out = await tool.ainvoke({"address": "0x123"})
    assert "not an EVM address" in out


async def test_adapter_failure_is_reported_to_the_model() -> None:
    tool = _by_name(chain_data_tools(BrokenChainData()))["get_address_profile"]
    out = await tool.ainvoke({"address": CLEAN})
    assert "get_address_profile failed: upstream down" in out


async def test_schema_rejects_out_of_range_limit() -> None:
    tool = _by_name(chain_data_tools(FakeChainData()))["get_recent_transfers"]
    with pytest.raises(ValueError):
        await tool.ainvoke({"address": CLEAN, "limit": 0})
