"""Human-in-the-loop review and final verdict nodes.

`human_review` pauses the graph with `interrupt()`. The caller resumes it with
`Command(resume=ReviewDecision(...))` (or its dict form) on the same thread_id;
the checkpointer makes this work across processes and graph rebuilds.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from sentinel.agents.state import TriageState
from sentinel.core.schemas import ReviewDecision, Verdict


def review_payload(state: TriageState, draft: Verdict) -> dict[str, Any]:
    """The compact view a reviewer needs to decide."""
    case = state["case"]
    return {
        "case_id": case.case_id,
        "address": case.address,
        "amount": case.amount,
        "asset": case.asset,
        "proposed_action": draft.action.value,
        "risk_level": draft.risk_level.value,
        "score": draft.score,
        "summary": draft.summary,
        "findings": [
            {"agent": f.agent, "risk_signal": f.risk_signal.value, "rationale": f.rationale}
            for f in draft.findings
        ],
        "critic_issues": state.get("critic_issues", []),
    }


def apply_decision(draft: Verdict, decision: ReviewDecision) -> Verdict:
    if decision.case_id != draft.case_id:
        raise ValueError(f"review is for case {decision.case_id!r}, not {draft.case_id!r}")
    if not decision.approve and decision.override_action is None:
        raise ValueError("a rejected review must set override_action")
    action = draft.action if decision.approve else decision.override_action
    assert action is not None
    summary = draft.summary + (f" Reviewer note: {decision.note}" if decision.note else "")
    return draft.model_copy(
        update={"action": action, "summary": summary, "reviewed_by": decision.reviewer}
    )


async def human_review(state: TriageState) -> dict[str, Any]:
    # Everything before interrupt() re-runs on resume, so keep it side-effect free.
    draft = state.get("draft_verdict")
    if draft is None:
        raise ValueError("human_review reached without a draft verdict")
    answer = interrupt(review_payload(state, draft))
    decision = ReviewDecision.model_validate(answer)
    return {"review_decision": decision, "verdict": apply_decision(draft, decision)}


async def finalize(state: TriageState) -> dict[str, Any]:
    return {"verdict": state.get("draft_verdict")}
