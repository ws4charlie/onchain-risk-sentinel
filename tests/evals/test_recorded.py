"""RecordedPorts replay and the recording script (with fake ports, no network)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.agents.tools import build_tools
from sentinel.core.ports import ChainDataSource, SanctionsSource, ScamIntelSource
from sentinel.core.schemas import Chain
from sentinel.evals.dataset import (
    EvalCase,
    Label,
    LabelSource,
    SnapshotOrigin,
    load_dataset,
)
from sentinel.evals.record import record_case, select_cases
from sentinel.evals.recorded import RecordedPorts, UnrecordedLookupError, offline_pattern_kb
from tests.fakes import CLEAN, DRAINER, FakeChainData, FakeSanctions, FakeScamIntel, fake_ports

UNKNOWN = "0x9999999999999999999999999999999999999999"


@pytest.fixture
def ports() -> RecordedPorts:
    dataset = load_dataset()
    return RecordedPorts(dataset.snapshots["ev-0001"], fake_ports().patterns)


def test_implements_every_port(ports: RecordedPorts) -> None:
    assert isinstance(ports.chain_data, ChainDataSource)
    assert isinstance(ports.sanctions, SanctionsSource)
    assert isinstance(ports.scam_intel, ScamIntelSource)


async def test_replays_recorded_answers(ports: RecordedPorts) -> None:
    snapshot = load_dataset().snapshots["ev-0001"]
    case = next(c for c in load_dataset().cases if c.case_id == "ev-0001")
    profile = await ports.chain_data.get_address_profile(
        Chain.ETHEREUM, "0x" + case.address[2:].upper()
    )
    assert profile == snapshot.profiles[case.address]
    transfers = await ports.chain_data.get_recent_transfers(Chain.ETHEREUM, case.address, 2)
    assert transfers == snapshot.transfers[case.address][:2]
    hit = await ports.sanctions.check(case.address)
    assert hit is not None and hit.list_name == "OFAC SDN"
    assert await ports.scam_intel.lookup(case.address) == []
    assert ports.misses == []


async def test_clean_counterparty_is_recorded_as_none(ports: RecordedPorts) -> None:
    snapshot = load_dataset().snapshots["ev-0001"]
    clean = next(a for a, hit in snapshot.sanctions.items() if hit is None)
    assert await ports.sanctions.check(clean) is None


async def test_unrecorded_lookups_raise_and_are_counted(ports: RecordedPorts) -> None:
    with pytest.raises(UnrecordedLookupError):
        await ports.chain_data.get_address_profile(Chain.ETHEREUM, UNKNOWN)
    with pytest.raises(UnrecordedLookupError):
        await ports.sanctions.check(UNKNOWN)
    with pytest.raises(UnrecordedLookupError):
        await ports.scam_intel.lookup(UNKNOWN)
    assert ports.misses == [f"profile:{UNKNOWN}", f"sanctions:{UNKNOWN}", f"scam_reports:{UNKNOWN}"]


async def test_agent_tools_turn_a_miss_into_an_error_without_evidence(
    ports: RecordedPorts,
) -> None:
    tool = build_tools(ports)["sanctions"][0]
    message = await tool.ainvoke(
        {"type": "tool_call", "name": tool.name, "id": "1", "args": {"address": UNKNOWN}}
    )
    assert "data source failed" in str(message.content)
    assert message.artifact == []


async def test_offline_pattern_kb_is_deterministic() -> None:
    first = await offline_pattern_kb().search("zero-value transfers from a look-alike address", 3)
    second = await offline_pattern_kb().search("zero-value transfers from a look-alike address", 3)
    assert [d.doc_id for d in first] == [d.doc_id for d in second]
    assert len(first) == 3


def _real_case(address: str, case_id: str = "ev-0500") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        address=address,
        label=Label(
            expected_action="block",
            min_risk_level="high",
            scenario="test",
            source=LabelSource.SCAM_FEED,
            rationale="Reported by a test feed.",
        ),
    )


async def test_record_case_covers_address_and_counterparties() -> None:
    snapshot = await record_case(
        _real_case(DRAINER),
        FakeChainData(),
        FakeSanctions(),
        FakeScamIntel(),
        sources={"chain_data": "fake"},
    )
    assert snapshot.origin is SnapshotOrigin.RECORDED
    assert snapshot.covers(DRAINER)
    peers = {t.from_address for t in snapshot.transfers[DRAINER]} - {DRAINER}
    assert peers and all(p in snapshot.sanctions and p in snapshot.profiles for p in peers)
    report = snapshot.scam_reports[DRAINER][0]
    assert report.category == "drainer"
    assert report.description == "Listed by the fixture feed as drainer."  # derived, not copied


def test_select_cases_skips_synthetic_and_recorded(tmp_path: Path) -> None:
    synthetic_case = load_dataset().cases[0]
    pending = _real_case(CLEAN, "ev-0501")
    done = _real_case(DRAINER, "ev-0502")
    (tmp_path / "ev-0502.json").write_text("{}")
    assert select_cases([synthetic_case, pending, done], [], tmp_path) == [pending]
    with pytest.raises(SystemExit):
        select_cases([synthetic_case], [synthetic_case.case_id], tmp_path)
