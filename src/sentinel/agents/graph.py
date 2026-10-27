"""Graph assembly and the run/resume helpers used by tests and entry points.

    START -> supervisor -> {chain_tracer, sanctions, scam_intel, pattern_analyst}* -> supervisor
          supervisor -> risk_scorer -> critic -> supervisor (send back) | human_review | finalize
          human_review -> END, finalize -> END

Routing out of `supervisor` and `critic` uses `Command(goto=...)`; their
possible destinations are declared so the compiled graph draws correctly.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from sentinel.agents.critic import make_critic
from sentinel.agents.models import Models
from sentinel.agents.review import finalize, human_review
from sentinel.agents.risk_scorer import make_risk_scorer
from sentinel.agents.specialists import make_specialist
from sentinel.agents.state import (
    SPECIALISTS,
    CriticRequest,
    RouteRecord,
    TriageState,
    initial_state,
)
from sentinel.agents.supervisor import SCORE_NODE, make_supervisor
from sentinel.agents.tools import build_prechecks, build_tools
from sentinel.core.config import Settings
from sentinel.core.ports import Ports
from sentinel.core.schemas import (
    Action,
    Case,
    Chain,
    Evidence,
    Finding,
    ReviewDecision,
    RiskLevel,
    Verdict,
)

TriageGraph = CompiledStateGraph[TriageState, None, TriageState, TriageState]

# Every non-builtin type stored in TriageState. Checkpoints only deserialize these.
_STATE_TYPES: tuple[type, ...] = (
    Action,
    Case,
    Chain,
    CriticRequest,
    Evidence,
    Finding,
    ReviewDecision,
    RiskLevel,
    RouteRecord,
    Verdict,
)


def checkpoint_serde() -> JsonPlusSerializer:
    """Checkpoint serializer that allowlists exactly the types in `TriageState`.

    Pass it to any checkpointer used with this graph (e.g. `InMemorySaver(serde=...)`).
    """
    return JsonPlusSerializer(
        allowed_msgpack_modules=[(t.__module__, t.__name__) for t in _STATE_TYPES]
    )


def build_graph(
    ports: Ports,
    models: Models,
    settings: Settings,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> TriageGraph:
    """Compile the triage graph.

    A checkpointer is required for human review; `InMemorySaver` is used when
    none is given (tests, one-off runs). Pass a persistent one in production,
    built with `serde=checkpoint_serde()`.
    """
    tools = build_tools(ports)
    prechecks = build_prechecks(ports)
    builder = StateGraph(TriageState)
    builder.add_node(
        "supervisor",
        make_supervisor(models.supervisor, settings),
        destinations=(*SPECIALISTS, SCORE_NODE),
    )
    for name in SPECIALISTS:
        builder.add_node(
            name, make_specialist(name, models.specialist, tools[name], prechecks.get(name))
        )
        builder.add_edge(name, "supervisor")
    builder.add_node(SCORE_NODE, make_risk_scorer(models.scorer, settings))
    builder.add_node(
        "critic",
        make_critic(models.critic, settings),
        destinations=("supervisor", "human_review", "finalize"),
    )
    builder.add_node("human_review", human_review)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "supervisor")
    builder.add_edge(SCORE_NODE, "critic")
    builder.add_edge("human_review", END)
    builder.add_edge("finalize", END)
    return builder.compile(
        checkpointer=checkpointer or InMemorySaver(serde=checkpoint_serde()), name="triage"
    )


@dataclass(frozen=True)
class TriageRun:
    """Where a run stopped: either a final verdict or a pending review request."""

    thread_id: str
    verdict: Verdict | None
    review_request: dict[str, Any] | None
    state: dict[str, Any] = field(repr=False)


def _config(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


async def _snapshot(graph: TriageGraph, thread_id: str) -> TriageRun:
    snapshot = await graph.aget_state(_config(thread_id))
    values: dict[str, Any] = dict(snapshot.values)
    request = snapshot.interrupts[0].value if snapshot.interrupts else None
    return TriageRun(
        thread_id=thread_id,
        verdict=values.get("verdict"),
        review_request=request,
        state=values,
    )


async def run_case(graph: TriageGraph, case: Case, *, thread_id: str | None = None) -> TriageRun:
    """Run a case until it finishes or pauses for human review."""
    thread_id = thread_id or f"{case.case_id}-{uuid.uuid4().hex[:8]}"
    await graph.ainvoke(initial_state(case), _config(thread_id))
    return await _snapshot(graph, thread_id)


async def resume_case(graph: TriageGraph, thread_id: str, decision: ReviewDecision) -> TriageRun:
    """Resume a paused case with the reviewer's decision."""
    await graph.ainvoke(Command(resume=decision), _config(thread_id))
    return await _snapshot(graph, thread_id)
