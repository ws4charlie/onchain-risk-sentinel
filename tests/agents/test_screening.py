"""Counterparty screening, flow features and the rules built on them."""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.agents import build_graph, run_case
from sentinel.agents.risk_scorer import apply_rules, withdrawal_is_unusual
from sentinel.agents.screening import (
    compute_flow_features,
    peel_hops,
    screen_counterparty_intel,
    screen_counterparty_sanctions,
)
from sentinel.agents.specialists import SpecialistAssessment
from sentinel.core.config import Settings
from sentinel.core.schemas import Action, Case, Evidence, Finding, RiskLevel, ScamReport
from tests.agents import world as fx
from tests.agents.conftest import make_models
from tests.agents.fake_llm import ScriptedChatModel, specialist_policy


def _finding(*evidence: Evidence) -> dict[str, Finding]:
    return {
        "x": Finding(
            agent="x",
            risk_signal=RiskLevel.MEDIUM,
            confidence=0.5,
            rationale="r",
            evidence=list(evidence),
        )
    }


def _evidence(kind: str, **data: Any) -> Evidence:
    return Evidence(source="t", kind=kind, summary=kind, data=data)


def _case(**context: Any) -> Case:
    return Case(
        case_id="c", address=fx.DEST, amount=10.0, requested_at=fx.NOW, account_context=context
    )


# --- evidence kinds ------------------------------------------------------------------------


async def test_counterparty_sanctions_hit_reports_direction_and_share() -> None:
    [hit] = await screen_counterparty_sanctions(fx.one_hop_sanctioned().ports(), fx.DEST)

    assert hit.kind == "counterparty_sanctions_hit"
    assert hit.data["direction"] == "in"
    assert hit.data["inflow"] == 60.0
    assert hit.data["inflow_share"] == pytest.approx(60 / 62, abs=1e-4)


async def test_counterparty_clear_when_nothing_found() -> None:
    w = fx.one_hop_sanctioned()
    w.sanctioned.clear()
    [clear] = await screen_counterparty_sanctions(w.ports(), fx.DEST)

    assert clear.kind == "counterparty_clear"
    assert len(clear.data["screened"]) == 2


async def test_counterparty_scam_report_is_reported() -> None:
    w = fx.one_hop_sanctioned()
    peer = fx.addr(0x91)
    w.reports[peer] = [ScamReport(address=peer, category="ponzi", source="feed")]
    evidence = await screen_counterparty_intel(w.ports(), fx.DEST)

    [report] = [e for e in evidence if e.kind == "counterparty_scam_report"]
    assert report.data["counterparty"] == peer
    assert report.data["category"] == "ponzi"


@pytest.mark.parametrize(
    "label", ["contract:TornadoCash_Eth", "contract:TornadoRouter", "Mixer: privacy pool"]
)
async def test_counterparty_mixer_matches_real_and_generic_labels(label: str) -> None:
    [mixer] = await screen_counterparty_intel(fx.mixer_funded(label).ports(), fx.DEST)

    assert mixer.kind == "counterparty_mixer"
    assert mixer.data["label"] == label
    assert mixer.data["inflow_share"] == 1.0


async def test_ordinary_contract_label_is_not_a_mixer() -> None:
    [clear] = await screen_counterparty_intel(
        fx.mixer_funded("contract:UniswapV2Router02").ports(), fx.DEST
    )
    assert clear.kind == "counterparty_clear"


async def test_flow_features_peel_chain() -> None:
    features = await compute_flow_features(fx.peel_chain().ports(), fx.DEST, fx.NOW)

    assert features.kind == "flow_features"
    assert features.data["peel_hops"] == 5
    assert features.data["peel_chain"] is True
    assert features.data["age_days"] == 3.0
    assert features.data["inflow_total"] == 200.0
    assert features.data["outflow_total"] == 104.0


def test_equal_split_is_not_a_peel_chain() -> None:
    w = fx.World()
    w.account(fx.DEST, age_days=3)
    w.send(fx.addr(1), fx.DEST, 100.0, days_ago=2)
    outs = [fx.addr(0x300 + i) for i in range(4)]
    for out in outs:
        w.send(fx.DEST, out, 25.0, days_ago=1)
    transfers = sorted(w.transfers[fx.DEST], key=lambda t: t.timestamp, reverse=True)
    # The first slice is 25% of 100, the second 25/75 = 33% > 30%: not peeling.
    assert peel_hops(fx.DEST, transfers, "ETH", set(outs)) == 1


async def test_flow_features_fresh_funders() -> None:
    features = await compute_flow_features(fx.fresh_funded().ports(), fx.DEST, fx.NOW)

    assert features.data["destination_fresh"] is True
    assert features.data["fresh_funder_count"] == 3
    assert features.data["fresh_inflow_share"] == 1.0
    assert features.data["peel_chain"] is False


# --- rules ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("share", "level", "score"), [(0.6, "high", 80), (0.2, "medium", 70)])
def test_rule_counterparty_sanction(share: float, level: str, score: int) -> None:
    outcome = apply_rules(_finding(_evidence("counterparty_sanctions_hit", exposure_share=share)))
    assert outcome is not None
    assert (outcome.rule, outcome.risk_level.value, outcome.score) == (
        "counterparty_sanction",
        level,
        score,
    )
    assert outcome.action is Action.HOLD_FOR_REVIEW and outcome.floor


def test_rule_ignores_sanctioned_dust() -> None:
    assert (
        apply_rules(_finding(_evidence("counterparty_sanctions_hit", exposure_share=0.01))) is None
    )


def test_rule_counterparty_mixer() -> None:
    outcome = apply_rules(_finding(_evidence("counterparty_mixer", exposure_share=0.5)))
    assert outcome is not None and (outcome.rule, outcome.score) == ("counterparty_mixer", 65)
    assert apply_rules(_finding(_evidence("counterparty_mixer", exposure_share=0.05))) is None


def test_rule_peel_chain() -> None:
    outcome = apply_rules(_finding(_evidence("flow_features", peel_chain=True)))
    assert outcome is not None and (outcome.rule, outcome.score) == ("peel_chain", 65)


def test_rule_fresh_inflow_needs_unusual_withdrawal() -> None:
    features = _evidence(
        "flow_features", destination_fresh=True, fresh_inflow_share=0.9, fresh_funder_count=3
    )
    outcome = apply_rules(_finding(features), _case(account_age_days=5))
    assert outcome is not None and (outcome.rule, outcome.score) == ("fresh_inflow", 60)
    assert apply_rules(_finding(features), _case(account_age_days=900)) is None


def test_withdrawal_is_unusual() -> None:
    assert withdrawal_is_unusual(_case(first_withdrawal=True))
    assert withdrawal_is_unusual(_case(typical_withdrawal_amount=2.0))  # 10 >= 3 x 2
    assert not withdrawal_is_unusual(_case(typical_withdrawal_amount=5.0))
    assert not withdrawal_is_unusual(_case())


def test_hard_rules_keep_priority() -> None:
    outcome = apply_rules(
        _finding(
            _evidence("lookalike_match"),
            _evidence("counterparty_sanctions_hit", exposure_share=0.9),
        )
    )
    assert outcome is not None and outcome.action is Action.BLOCK


# --- graph scenarios -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("world", "context", "rule_score"),
    [
        (fx.one_hop_sanctioned, {}, 80),
        (fx.mixer_funded, {}, 65),
        (fx.peel_chain, {}, 65),
        (fx.fresh_funded, {"account_age_days": 5, "first_withdrawal": True}, 60),
    ],
    ids=["sanctioned_one_hop", "mixer_exposure", "peel_chain", "fresh_large_inflow"],
)
async def test_indirect_risk_scenarios_hold_for_review(
    world: Any, context: dict[str, Any], rule_score: int, settings: Settings
) -> None:
    # The scripted scorer proposes allow/10; the floor rules must lift it to a hold.
    graph = build_graph(world().ports(), make_models(), settings)
    run = await run_case(graph, _case(**context))

    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "hold_for_review"
    assert run.review_request["score"] == rule_score


async def test_floor_rule_lets_the_scorer_escalate(settings: Settings) -> None:
    from sentinel.agents.risk_scorer import ScoreAssessment
    from tests.agents.fake_llm import queued

    block = {"risk_level": "high", "score": 90, "action": "block", "summary": "Escalated."}
    models = make_models()
    models = type(models)(
        supervisor=models.supervisor,
        specialist=ScriptedChatModel(policy=specialist_policy(SpecialistAssessment)),
        scorer=ScriptedChatModel(policy=queued(ScoreAssessment, [], block)),
        critic=models.critic,
    )
    run = await run_case(build_graph(fx.mixer_funded().ports(), models, settings), _case())

    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "block"
    assert run.review_request["score"] == 90


async def test_prechecks_land_in_findings_without_tool_calls(settings: Settings) -> None:
    # A specialist that answers immediately, without calling any tool.
    from tests.agents.fake_llm import answer

    def lazy(messages: Any, tools: list[str]) -> Any:
        return answer(SpecialistAssessment, risk_signal="none", confidence=0.5, rationale="ok")

    models = make_models()
    models = type(models)(
        supervisor=models.supervisor,
        specialist=ScriptedChatModel(policy=lazy),
        scorer=models.scorer,
        critic=models.critic,
    )
    run = await run_case(build_graph(fx.peel_chain().ports(), models, settings), _case())

    kinds = {e.kind for f in run.state["findings"].values() for e in f.evidence}
    assert {"flow_features", "lookalike_clear", "counterparty_clear"} <= kinds
    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "hold_for_review"
    prompt = str(models.specialist.calls[0][-1].content)  # type: ignore[attr-defined]
    assert "Deterministic checks already run" in prompt
