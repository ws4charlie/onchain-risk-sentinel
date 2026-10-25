"""Critic: checks the draft verdict's claims against the cited evidence.

Two layers:
1. Deterministic: a finding that signals medium risk or more without any
   evidence is rejected outright (no LLM call needed).
2. LLM: reads the summary and rationales next to the evidence and lists
   unsupported claims.

A rejection sends the case back to the supervisor with a `CriticRequest`, at
most `settings.max_revisions` times. After that the draft goes forward and the
unresolved issues stay in `critic_issues` for the reviewer and the audit log.
"""

from __future__ import annotations

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from sentinel.agents import prompts
from sentinel.agents.state import SPECIALISTS, CriticRequest, Node, SpecialistName, TriageState
from sentinel.agents.structured import ask_structured
from sentinel.core.config import Settings
from sentinel.core.schemas import RiskLevel, Verdict

_NEEDS_EVIDENCE = {RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL}


class Critique(BaseModel):
    """The critic LLM's structured answer."""

    approved: bool
    issues: list[str] = Field(default_factory=list, description="Unsupported claims, if any.")
    rerun: list[SpecialistName] = Field(
        default_factory=list, description="Specialists that must re-check."
    )
    instruction: str = Field(default="", description="One precise instruction for them.")


def check_evidence(draft: Verdict) -> Critique:
    """Deterministic check: risky findings must cite evidence."""
    bare = [f for f in draft.findings if f.risk_signal in _NEEDS_EVIDENCE and not f.evidence]
    if not bare:
        return Critique(approved=True)
    return Critique(
        approved=False,
        issues=[f"{f.agent} claims {f.risk_signal.value} risk without evidence" for f in bare],
        rerun=[f.agent for f in bare if f.agent in SPECIALISTS],
        instruction="Call your tools and cite the evidence for your risk signal.",
    )


def make_critic(model: BaseChatModel, settings: Settings) -> Node[Command[str]]:
    async def critic(state: TriageState) -> Command[str]:
        draft = state.get("draft_verdict")
        if draft is None:
            raise ValueError("critic reached without a draft verdict")

        critique = check_evidence(draft)
        if critique.approved:
            critique = await ask_structured(
                model,
                Critique,
                [SystemMessage(prompts.CRITIC), HumanMessage(prompts.render_critic_input(draft))],
            )

        revisions = state.get("revision_count", 0)
        if not critique.approved and revisions < settings.max_revisions:
            request = CriticRequest(
                specialists=list(dict.fromkeys(critique.rerun)),
                instruction=critique.instruction or "; ".join(critique.issues),
            )
            return Command(
                goto="supervisor",
                update={
                    "revision_count": revisions + 1,
                    "critic_request": request,
                    "critic_issues": critique.issues,
                },
            )

        goto = "human_review" if draft.needs_human_review else "finalize"
        issues = [] if critique.approved else critique.issues
        return Command(goto=goto, update={"critic_issues": issues})

    return critic
