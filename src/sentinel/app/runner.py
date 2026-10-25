"""Streaming run/resume on top of the compiled graph, shared by the CLI and the API.

Progress comes from `graph.astream(stream_mode="updates")`: one `ProgressEvent` per
node update, then one terminal event (`paused`, `completed` or `failed`). The final
state is always read back from the checkpointer, so a paused case can be resumed by
any process that shares it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel, Field

from sentinel.agents.state import RouteRecord, initial_state
from sentinel.app.wiring import Runtime
from sentinel.core.schemas import Case, Finding, ReviewDecision, Verdict


class CaseStatus(StrEnum):
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"  # checkpoint has pending work but no live run (e.g. a crash)


EventKind = Literal["node", "paused", "completed", "failed"]
TERMINAL_KINDS: frozenset[str] = frozenset({"paused", "completed", "failed"})


class ProgressEvent(BaseModel):
    thread_id: str
    kind: EventKind
    node: str | None = None
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class CaseState(BaseModel):
    """A case as read from its checkpoint."""

    thread_id: str
    case_id: str
    status: CaseStatus
    verdict: Verdict | None = None
    review_request: dict[str, Any] | None = None


def new_thread_id(case_id: str) -> str:
    return f"{case_id}-{uuid.uuid4().hex[:8]}"


def run_config(runtime: Runtime, thread_id: str, case_id: str) -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id},
        "callbacks": list(runtime.callbacks),
        "run_name": "triage",
        "metadata": {
            "case_id": case_id,
            "thread_id": thread_id,
            "langfuse_session_id": case_id,
            "langfuse_tags": ["triage"],
        },
    }


def describe(node: str, update: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A one-line, human-readable summary of a node update plus a JSON-safe payload."""
    data: dict[str, Any] = {}
    parts: list[str] = []
    history: list[RouteRecord] = update.get("routing_history") or []
    for record in history:
        targets = ", ".join(record.targets) or "risk_scorer"
        parts.append(f"route -> {targets} ({record.reason})")
        data.setdefault("routes", []).append(record.model_dump(mode="json"))
    findings: dict[str, Finding] = update.get("findings") or {}
    for finding in findings.values():
        parts.append(
            f"{finding.agent}: {finding.risk_signal.value} "
            f"(confidence {finding.confidence:.2f}, {len(finding.evidence)} evidence)"
        )
        data.setdefault("findings", []).append(
            {
                "agent": finding.agent,
                "risk_signal": finding.risk_signal.value,
                "confidence": finding.confidence,
                "evidence": len(finding.evidence),
            }
        )
    for key, label in (("draft_verdict", "draft"), ("verdict", "verdict")):
        verdict: Verdict | None = update.get(key)
        if verdict is not None:
            parts.append(
                f"{label}: {verdict.action.value}, {verdict.risk_level.value}, "
                f"score {verdict.score}"
            )
            data[key] = {
                "action": verdict.action.value,
                "risk_level": verdict.risk_level.value,
                "score": verdict.score,
            }
    if "critic_issues" in update:
        issues: list[str] = update["critic_issues"] or []
        parts.append(f"critic: {len(issues)} open issue(s)" if issues else "critic: approved")
        data["critic_issues"] = issues
    if update.get("critic_request") is not None:
        parts.append("critic sent the case back")
    return ("; ".join(parts) or "done"), data


async def read_case(runtime: Runtime, thread_id: str) -> CaseState | None:
    """The case's state from its checkpoint, or None if the thread does not exist."""
    snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": thread_id}})
    values: dict[str, Any] = dict(snapshot.values or {})
    case: Case | None = values.get("case")
    if case is None:
        return None
    verdict: Verdict | None = values.get("verdict")
    request = snapshot.interrupts[0].value if snapshot.interrupts else None
    if request is not None:
        status = CaseStatus.AWAITING_REVIEW
    elif verdict is not None and not snapshot.next:
        status = CaseStatus.COMPLETED
    else:
        status = CaseStatus.INTERRUPTED
    return CaseState(
        thread_id=thread_id,
        case_id=case.case_id,
        status=status,
        verdict=verdict,
        review_request=request,
    )


def check_decision(state: CaseState, decision: ReviewDecision) -> str | None:
    """Why `decision` cannot resume `state`, or None if it can."""
    if state.status is not CaseStatus.AWAITING_REVIEW:
        return f"case is {state.status.value}, not awaiting review"
    if decision.case_id != state.case_id:
        return f"review is for case {decision.case_id!r}, but this thread is {state.case_id!r}"
    if not decision.approve and decision.override_action is None:
        return "a rejected review must set override_action"
    return None


def terminal_event(state: CaseState) -> ProgressEvent:
    if state.status is CaseStatus.AWAITING_REVIEW:
        assert state.review_request is not None
        return ProgressEvent(
            thread_id=state.thread_id,
            kind="paused",
            detail=f"paused for human review: proposed {state.review_request['proposed_action']}",
            data={"review_request": state.review_request},
        )
    if state.status is CaseStatus.COMPLETED:
        assert state.verdict is not None
        return ProgressEvent(
            thread_id=state.thread_id,
            kind="completed",
            detail=f"verdict: {state.verdict.action.value}",
            data={"verdict": state.verdict.model_dump(mode="json")},
        )
    return ProgressEvent(
        thread_id=state.thread_id, kind="failed", detail=f"run stopped in state {state.status}"
    )


async def _stream(
    runtime: Runtime, graph_input: Any, thread_id: str, case_id: str
) -> AsyncIterator[ProgressEvent]:
    config = run_config(runtime, thread_id, case_id)
    try:
        async for chunk in runtime.graph.astream(graph_input, config, stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__" or not isinstance(update, dict):
                    continue
                detail, data = describe(node, update)
                yield ProgressEvent(
                    thread_id=thread_id, kind="node", node=node, detail=detail, data=data
                )
    except Exception as exc:
        yield ProgressEvent(
            thread_id=thread_id,
            kind="failed",
            detail=f"{type(exc).__name__}: {exc}",
            data={"error": type(exc).__name__},
        )
        return
    state = await read_case(runtime, thread_id)
    assert state is not None
    yield terminal_event(state)


def stream_case(runtime: Runtime, case: Case, thread_id: str) -> AsyncIterator[ProgressEvent]:
    """Run a new case, yielding progress until it completes, pauses or fails."""
    return _stream(runtime, initial_state(case), thread_id, case.case_id)


def stream_resume(
    runtime: Runtime, thread_id: str, decision: ReviewDecision
) -> AsyncIterator[ProgressEvent]:
    """Resume a paused case with a reviewer's decision, yielding progress."""
    return _stream(runtime, Command(resume=decision), thread_id, decision.case_id)
