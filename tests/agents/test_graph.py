"""End-to-end graph scenarios against fake ports and scripted models."""

from __future__ import annotations

import pytest

from sentinel.agents import build_graph, resume_case, run_case
from sentinel.core.config import Settings
from sentinel.core.schemas import Action, Case, ReviewDecision, RiskLevel
from tests.agents.conftest import GraphFactory, make_models
from tests.fakes import CLEAN, DRAINER, SANCTIONED, fake_ports


def _case(address: str, case_id: str = "case-1") -> Case:
    return Case(case_id=case_id, address=address, amount=1.5)


async def test_clean_address_is_allowed_without_review(graph_factory: GraphFactory) -> None:
    run = await run_case(graph_factory(), _case(CLEAN))

    assert run.review_request is None
    assert run.verdict is not None
    assert run.verdict.action is Action.ALLOW
    assert not run.verdict.needs_human_review
    agents = {f.agent for f in run.verdict.findings}
    assert agents == {"chain_tracer", "sanctions", "scam_intel", "pattern_analyst"}
    assert all(f.evidence for f in run.verdict.findings)
    kinds = {e.kind for f in run.verdict.findings for e in f.evidence}
    assert {"address_profile", "recent_transfers", "sanctions_clear"} <= kinds
    history = run.state["routing_history"]
    assert [r.targets for r in history][-1] == []


async def test_sanctioned_address_pauses_then_blocks_on_approval(
    graph_factory: GraphFactory,
) -> None:
    graph = graph_factory()
    run = await run_case(graph, _case(SANCTIONED))

    assert run.verdict is None
    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "block"
    assert run.review_request["risk_level"] == "critical"
    assert run.review_request["score"] == 100

    decision = ReviewDecision(case_id="case-1", reviewer="analyst-1", approve=True)
    final = await resume_case(graph, run.thread_id, decision)

    assert final.review_request is None
    assert final.verdict is not None
    assert final.verdict.action is Action.BLOCK
    assert final.verdict.risk_level is RiskLevel.CRITICAL
    assert final.verdict.reviewed_by == "analyst-1"
    assert final.state["review_decision"] == decision


async def test_hard_rule_overrides_llm_scorer(graph_factory: GraphFactory) -> None:
    # The scripted scorer always proposes low/allow; the scam_report rule must win.
    run = await run_case(graph_factory(), _case(DRAINER))

    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "block"
    assert run.review_request["risk_level"] == "high"


async def test_critic_sends_back_once_then_approves(graph_factory: GraphFactory) -> None:
    critiques = [
        {
            "approved": False,
            "issues": ["Address age is not cited."],
            "rerun": ["chain_tracer"],
            "instruction": "Cite the first-seen date.",
        }
    ]
    routes = [
        {"specialists": ["chain_tracer", "sanctions", "scam_intel"], "reason": "checks"},
        {"specialists": ["pattern_analyst"], "reason": "patterns"},
        # All four have run, so the guard sends the case to scoring without asking the model.
        {"specialists": ["chain_tracer", "sanctions"], "reason": "critic asked; sanctions too"},
    ]
    models = make_models(routes=routes, critiques=critiques)
    run = await run_case(graph_factory(models), _case(CLEAN))

    assert run.verdict is not None
    assert run.verdict.action is Action.ALLOW
    assert run.state["revision_count"] == 1
    assert run.state["critic_issues"] == []
    history = run.state["routing_history"]
    rerun = [r for r in history if r.revision == 1 and r.targets]
    # Only chain_tracer may re-run: sanctions already ran and the critic did not ask for it.
    assert [r.targets for r in rerun] == [["chain_tracer"]]
    assert "guard dropped repeat calls: sanctions" in rerun[0].reason
    # The specialist saw the critic's instruction.
    specialist_inputs = [str(c[-1].content) for c in models.specialist.calls]  # type: ignore[attr-defined]
    assert any("Cite the first-seen date." in text for text in specialist_inputs)


async def test_critic_send_backs_are_capped_by_max_revisions(
    graph_factory: GraphFactory,
) -> None:
    reject = {"approved": False, "issues": ["Unsupported."], "rerun": ["scam_intel"]}
    models = make_models(critiques=[reject, reject, reject])
    run = await run_case(graph_factory(models, max_revisions=1), _case(CLEAN))

    assert run.verdict is not None
    assert run.state["revision_count"] == 1
    assert run.state["critic_issues"] == ["Unsupported."]


async def test_repeat_call_without_critic_request_goes_to_scoring(
    graph_factory: GraphFactory,
) -> None:
    routes = [
        {"specialists": ["sanctions"], "reason": "screen first"},
        {"specialists": ["sanctions"], "reason": "screen again"},
    ]
    run = await run_case(graph_factory(make_models(routes=routes)), _case(CLEAN))

    assert run.verdict is not None
    last = run.state["routing_history"][-1]
    assert last.targets == [] and last.forced
    assert set(run.state["findings"]) == {"sanctions"}


async def test_max_steps_guard_still_produces_a_verdict(graph_factory: GraphFactory) -> None:
    # The supervisor would keep routing forever; the guard stops it after two decisions.
    routes = [
        {"specialists": ["sanctions"], "reason": "one"},
        {"specialists": ["scam_intel"], "reason": "two"},
        {"specialists": ["chain_tracer"], "reason": "three"},
        {"specialists": ["pattern_analyst"], "reason": "four"},
    ]
    models = make_models(routes=routes)
    run = await run_case(graph_factory(models, max_steps=2), _case(CLEAN))

    assert run.verdict is not None
    assert run.state["step_count"] == 2
    assert set(run.state["findings"]) == {"sanctions", "scam_intel"}
    last = run.state["routing_history"][-1]
    assert last.forced and "max_steps=2" in last.reason


async def test_resume_from_checkpoint_with_rebuilt_graph(settings: Settings, saver) -> None:  # type: ignore[no-untyped-def]
    first = build_graph(fake_ports(), make_models(), settings, saver)
    paused = await run_case(first, _case(SANCTIONED, "case-ckpt"), thread_id="thread-ckpt")
    assert paused.review_request is not None

    # A new process would rebuild everything; only the checkpointer and thread_id survive.
    rebuilt = build_graph(fake_ports(), make_models(), settings, saver)
    decision = ReviewDecision(
        case_id="case-ckpt",
        reviewer="analyst-2",
        approve=False,
        override_action=Action.HOLD_FOR_REVIEW,
        note="Escalate to compliance.",
    )
    final = await resume_case(rebuilt, "thread-ckpt", decision)

    assert final.verdict is not None
    assert final.verdict.action is Action.HOLD_FOR_REVIEW
    assert final.verdict.reviewed_by == "analyst-2"
    assert final.verdict.summary.endswith("Reviewer note: Escalate to compliance.")


async def test_review_for_wrong_case_is_rejected(graph_factory: GraphFactory) -> None:
    graph = graph_factory()
    run = await run_case(graph, _case(SANCTIONED))
    wrong = ReviewDecision(case_id="other", reviewer="analyst-1", approve=True)

    with pytest.raises(ValueError, match="not 'case-1'"):
        await resume_case(graph, run.thread_id, wrong)
