"""Supervisor: decides which specialists run next, or that the case is ready to score.

The LLM proposes; deterministic guards dispose:
- after `settings.max_steps` decisions the case goes to scoring without an LLM call;
- a specialist that already ran is never called again, unless the critic asked
  for it in the current revision round and it has not re-run yet;
- if the guards leave nothing to call, the case goes to scoring;
- mandatory screening: whenever the case would go to scoring (LLM choice or a
  guard), any of `MANDATORY` that has not run yet runs first. Compliance checks
  never depend on a model's choice, and they run even after `max_steps`.
Several specialists in one decision run in parallel (one LangGraph superstep).
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from sentinel.agents import prompts
from sentinel.agents.state import SPECIALISTS, Node, RouteRecord, SpecialistName, TriageState
from sentinel.agents.structured import ask_structured
from sentinel.core.config import Settings

SCORE_NODE = "risk_scorer"
MANDATORY: tuple[SpecialistName, ...] = ("sanctions", "scam_intel")


class RoutingDecision(BaseModel):
    """The supervisor's structured answer."""

    specialists: list[SpecialistName] = Field(
        description="Specialists to call next, in parallel. Empty means: go to scoring."
    )
    reason: str = Field(description="One sentence explaining the choice.")


def available_specialists(state: TriageState) -> list[SpecialistName]:
    """Specialists the supervisor may call now (the no-repeat guard)."""
    history = state.get("routing_history", [])
    revision = state.get("revision_count", 0)
    already_ran = {name for record in history for name in record.targets}
    rerun_done = {
        name for record in history if record.revision == revision for name in record.targets
    }
    request = state.get("critic_request")
    rerun = set(request.specialists) - rerun_done if request else set()
    return [name for name in SPECIALISTS if name not in already_ran or name in rerun]


def _route(
    state: TriageState,
    targets: list[SpecialistName],
    reason: str,
    *,
    forced: bool,
    llm_called: bool,
) -> Command[str]:
    step = state.get("step_count", 0)
    record = RouteRecord(
        step=step,
        revision=state.get("revision_count", 0),
        targets=targets,
        reason=reason,
        forced=forced,
    )
    goto: str | list[str] = list(targets) if targets else SCORE_NODE
    update: dict[str, object] = {"routing_history": [record]}
    if llm_called:
        update["step_count"] = step + 1
    return Command(goto=goto, update=update)


def missing_mandatory(state: TriageState) -> list[SpecialistName]:
    """Mandatory specialists that have not run yet in this case."""
    ran = {name for record in state.get("routing_history", []) for name in record.targets}
    return [name for name in MANDATORY if name not in ran]


def _to_scoring(state: TriageState, reason: str, *, forced: bool, llm_called: bool) -> Command[str]:
    """Go to scoring, unless mandatory screening is still missing; then run that first."""
    missing = missing_mandatory(state)
    if missing:
        reason = f"{reason}; mandatory screening first: {', '.join(missing)}"
        return _route(state, missing, reason, forced=True, llm_called=llm_called)
    return _route(state, [], reason, forced=forced, llm_called=llm_called)


def make_supervisor(model: BaseChatModel, settings: Settings) -> Node[Command[str]]:
    async def supervisor(state: TriageState) -> Command[str]:
        if state.get("step_count", 0) >= settings.max_steps:
            return _to_scoring(
                state, f"max_steps={settings.max_steps} reached", forced=True, llm_called=False
            )
        available = available_specialists(state)
        if not available:
            return _to_scoring(state, "no specialist left to call", forced=True, llm_called=False)

        decision = await ask_structured(
            model,
            RoutingDecision,
            [
                SystemMessage(prompts.SUPERVISOR),
                HumanMessage(
                    prompts.render_supervisor_input(
                        state["case"],
                        state.get("findings", {}),
                        available,
                        state.get("critic_request"),
                    )
                ),
            ],
        )
        targets = list(dict.fromkeys(s for s in decision.specialists if s in available))
        dropped = sorted(set(decision.specialists) - set(targets))
        reason = decision.reason
        if dropped:
            reason += f" (guard dropped repeat calls: {', '.join(dropped)})"
        if not targets:
            return _to_scoring(state, reason, forced=bool(dropped), llm_called=True)
        return _route(state, targets, reason, forced=False, llm_called=True)

    return supervisor
