"""Typed state shared by every node of the triage graph.

Reducers matter because specialists can run in parallel in one superstep:
`findings` merges per-specialist dicts and `routing_history` appends. Every
other key has a single writer, so it uses the default "last value wins".
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, NotRequired, Protocol, Required, TypedDict, get_args

from pydantic import BaseModel, ConfigDict, Field

from sentinel.core.schemas import Case, Finding, ReviewDecision, Verdict

SpecialistName = Literal["chain_tracer", "sanctions", "scam_intel", "pattern_analyst"]
SPECIALISTS: tuple[SpecialistName, ...] = get_args(SpecialistName)


class RouteRecord(BaseModel):
    """One supervisor decision, kept for audit and for the no-repeat guard."""

    model_config = ConfigDict(frozen=True)

    step: int
    revision: int = Field(description="Critic revision round in which the decision was made.")
    targets: list[SpecialistName] = Field(description="Specialists called; empty means score.")
    reason: str
    forced: bool = Field(default=False, description="True when a guard overrode the LLM.")


class CriticRequest(BaseModel):
    """What the critic wants re-checked before it approves the draft."""

    model_config = ConfigDict(frozen=True)

    specialists: list[SpecialistName]
    instruction: str


def merge_findings(left: dict[str, Finding], right: dict[str, Finding]) -> dict[str, Finding]:
    """Merge findings by specialist; a re-run replaces that specialist's earlier finding."""
    return {**left, **right}


class TriageState(TypedDict):
    case: Required[Case]
    findings: NotRequired[Annotated[dict[str, Finding], merge_findings]]
    routing_history: NotRequired[Annotated[list[RouteRecord], operator.add]]
    step_count: NotRequired[int]
    revision_count: NotRequired[int]
    critic_request: NotRequired[CriticRequest | None]
    critic_issues: NotRequired[list[str]]
    draft_verdict: NotRequired[Verdict | None]
    verdict: NotRequired[Verdict | None]
    review_decision: NotRequired[ReviewDecision | None]


def initial_state(case: Case) -> TriageState:
    return TriageState(
        case=case,
        findings={},
        routing_history=[],
        step_count=0,
        revision_count=0,
        critic_request=None,
        critic_issues=[],
        draft_verdict=None,
        verdict=None,
        review_decision=None,
    )


class Node[R](Protocol):
    """An async graph node. Named `state` parameter, as LangGraph's node protocol expects."""

    async def __call__(self, state: TriageState) -> R: ...
