"""Unit tests for the deterministic parts: rules, guards, evidence handling."""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import AIMessage, BaseMessage

from sentinel.agents.critic import check_evidence
from sentinel.agents.prompts import render_case
from sentinel.agents.risk_scorer import apply_rules
from sentinel.agents.specialists import SPECIALIST_RECURSION_LIMIT, make_specialist
from sentinel.agents.state import CriticRequest, RouteRecord, TriageState
from sentinel.agents.supervisor import available_specialists
from sentinel.agents.tools import _json_safe, build_tools, find_lookalikes
from sentinel.core.schemas import Action, Case, Evidence, Finding, RiskLevel, Verdict
from tests.agents.fake_llm import ScriptedChatModel, tool_call
from tests.fakes import CLEAN, FRESH, POISONING_LOOKALIKE, fake_ports


def _finding(agent: str, signal: RiskLevel, *evidence: Evidence) -> Finding:
    return Finding(
        agent=agent, risk_signal=signal, confidence=0.8, rationale="r", evidence=list(evidence)
    )


def _evidence(kind: str, **data: object) -> Evidence:
    return Evidence(source="test", kind=kind, summary=kind, data=dict(data))


def test_rules_sanctions_hit_wins() -> None:
    findings = {
        "sanctions": _finding("sanctions", RiskLevel.CRITICAL, _evidence("sanctions_hit")),
        "scam_intel": _finding(
            "scam_intel", RiskLevel.HIGH, _evidence("scam_report", category="drainer")
        ),
    }
    outcome = apply_rules(findings)
    assert outcome is not None
    assert (outcome.rule, outcome.action, outcome.score) == ("sanctions_hit", Action.BLOCK, 100)


def test_rules_no_findings_holds_for_review() -> None:
    outcome = apply_rules({})
    assert outcome is not None
    assert outcome.action is Action.HOLD_FOR_REVIEW


def test_evidence_check_rejects_risky_finding_without_evidence() -> None:
    draft = Verdict(
        case_id="c",
        risk_level=RiskLevel.HIGH,
        score=80,
        action=Action.BLOCK,
        summary="s",
        findings=[
            _finding("scam_intel", RiskLevel.HIGH),
            _finding("sanctions", RiskLevel.NONE),
        ],
    )
    critique = check_evidence(draft)
    assert not critique.approved
    assert critique.rerun == ["scam_intel"]


def test_available_specialists_allows_only_requested_reruns() -> None:
    case = Case(case_id="c", address=CLEAN)
    state = TriageState(
        case=case,
        revision_count=1,
        critic_request=CriticRequest(specialists=["sanctions"], instruction="i"),
        routing_history=[
            RouteRecord(step=0, revision=0, targets=["sanctions", "scam_intel"], reason="r")
        ],
    )
    assert available_specialists(state) == ["chain_tracer", "sanctions", "pattern_analyst"]

    state["routing_history"] = [
        *state["routing_history"],
        RouteRecord(step=1, revision=1, targets=["sanctions"], reason="rerun"),
    ]
    assert available_specialists(state) == ["chain_tracer", "pattern_analyst"]


async def test_tools_attach_evidence_artifacts() -> None:
    tools = build_tools(fake_ports())
    transfers = tools["chain_tracer"][1]
    message = await transfers.ainvoke(
        {
            "type": "tool_call",
            "name": transfers.name,
            "id": "1",
            "args": {"address": POISONING_LOOKALIKE.upper().replace("0X", "0x")},
        }
    )
    [evidence] = message.artifact
    assert evidence.kind == "recent_transfers"
    assert evidence.data["zero_value_count"] == 2


async def test_tools_report_invalid_address_without_evidence() -> None:
    check = build_tools(fake_ports())["sanctions"][0]
    message = await check.ainvoke(
        {"type": "tool_call", "name": check.name, "id": "1", "args": {"address": "nope"}}
    )
    assert message.artifact == []
    assert "not a valid EVM address" in message.content


async def test_tools_report_source_failure_without_evidence() -> None:
    class BrokenSanctions:
        async def check(self, address: str) -> None:
            raise TimeoutError("list unavailable")

    ports = fake_ports()
    ports.sanctions = BrokenSanctions()
    check = build_tools(ports)["sanctions"][0]
    message = await check.ainvoke(
        {"type": "tool_call", "name": check.name, "id": "1", "args": {"address": CLEAN}}
    )
    assert message.artifact == []
    assert "data source failed (TimeoutError: list unavailable)" in message.content


async def test_specialist_loop_limit_yields_zero_confidence_finding() -> None:
    def never_answers(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
        return tool_call("check_sanctions", {"address": CLEAN})

    model = ScriptedChatModel(policy=never_answers)
    node = make_specialist("sanctions", model, build_tools(fake_ports())["sanctions"])
    update = await node(TriageState(case=Case(case_id="c", address=CLEAN)))

    finding = update["findings"]["sanctions"]
    assert finding.confidence == 0.0
    assert str(SPECIALIST_RECURSION_LIMIT) in finding.rationale


def test_rules_lookalike_blocks() -> None:
    findings = {
        "chain_tracer": _finding("chain_tracer", RiskLevel.HIGH, _evidence("lookalike_match"))
    }
    outcome = apply_rules(findings)
    assert outcome is not None
    assert (outcome.rule, outcome.action, outcome.score) == ("lookalike", Action.BLOCK, 85)


def test_lookalike_match_reports_common_prefix_and_suffix() -> None:
    [match] = find_lookalikes(
        POISONING_LOOKALIKE, {"recent_withdrawal_addresses": [CLEAN.upper().replace("0X", "0x")]}
    )
    assert match.kind == "lookalike_match"
    # CLEAN = 0x1111111111...1a2b, look-alike = 0x1111999999...91a2b
    assert match.data["common_prefix_len"] == 4
    assert match.data["common_suffix_len"] == 4
    assert match.data["known_source"] == "recent_withdrawal_addresses"


def test_lookalike_accepts_address_book_entries() -> None:
    [match] = find_lookalikes(
        POISONING_LOOKALIKE, {"address_book": [{"label": "savings", "address": CLEAN}, 7]}
    )
    assert match.data["known_source"] == "address_book"


def test_exact_known_address_is_not_a_lookalike() -> None:
    [evidence] = find_lookalikes(CLEAN, {"recent_withdrawal_addresses": [CLEAN]})
    assert evidence.kind == "lookalike_clear"
    assert evidence.data["exact_known_match"] is True


def test_no_account_context_is_clear() -> None:
    [evidence] = find_lookalikes(FRESH, {})
    assert evidence.kind == "lookalike_clear"
    assert evidence.data["known_count"] == 0


def test_json_safe_stringifies_only_out_of_range_ints() -> None:
    data = {"a": 2**63 - 1, "b": 2**63, "c": [-(2**63) - 1, True, 1.5], "d": {"e": 3 * 10**19}}
    assert _json_safe(data) == {
        "a": 2**63 - 1,
        "b": str(2**63),
        "c": [str(-(2**63) - 1), True, 1.5],
        "d": {"e": str(3 * 10**19)},
    }


def test_rendered_case_states_request_time() -> None:
    case = Case(case_id="c", address=CLEAN, requested_at=datetime(2026, 10, 20, 12, tzinfo=UTC))
    assert "Requested at: 2026-10-20T12:00:00+00:00" in render_case(case)


def test_rules_hold_nonblocking_scam_categories() -> None:
    findings = {
        "scam_intel": _finding(
            "scam_intel", RiskLevel.MEDIUM, _evidence("scam_report", category="ponzi")
        )
    }
    outcome = apply_rules(findings)
    assert outcome is not None
    assert outcome.rule == "scam_report_review"
    assert outcome.action is Action.HOLD_FOR_REVIEW and outcome.floor


def test_user_notice_for_victim_facing_risks_only() -> None:
    from sentinel.agents.notices import user_notice

    assert "investment scheme" in (user_notice([_evidence("scam_report", category="ponzi")]) or "")
    assert "look-alike" in (user_notice([_evidence("lookalike_match")]) or "")
    assert user_notice([_evidence("scam_report", category="unknown")]) is not None
    assert user_notice([_evidence("sanctions_hit"), _evidence("counterparty_mixer")]) is None
