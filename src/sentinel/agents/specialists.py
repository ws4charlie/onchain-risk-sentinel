"""Specialist agents: each is a small ReAct agent with only its own tools.

The model decides which of its tools to call and when to stop, then answers
with a structured `SpecialistAssessment`. The node turns that into a `Finding`
whose evidence comes from the tool artifacts (see `sentinel.agents.tools`),
not from the model's text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from pydantic import BaseModel, Field

from sentinel.agents import prompts
from sentinel.agents.state import Node, SpecialistName, TriageState
from sentinel.core.schemas import Evidence, Finding, RiskLevel

# Bounds one specialist's ReAct loop (model and tool steps), independent of the
# supervisor's max_steps. Hitting it yields a zero-confidence finding, not a crash.
SPECIALIST_RECURSION_LIMIT = 12


class SpecialistAssessment(BaseModel):
    """A specialist's structured final answer."""

    risk_signal: RiskLevel
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="Facts from the tool results that justify the signal.")


def collect_evidence(messages: Sequence[BaseMessage]) -> list[Evidence]:
    """Return the evidence carried by tool artifacts, in call order, without duplicates."""
    seen: set[str] = set()
    evidence: list[Evidence] = []
    for message in messages:
        if not isinstance(message, ToolMessage) or not isinstance(message.artifact, list):
            continue
        for item in message.artifact:
            if not isinstance(item, Evidence):
                continue
            key = json.dumps(item.model_dump(mode="json", exclude={"observed_at"}), sort_keys=True)
            if key not in seen:
                seen.add(key)
                evidence.append(item)
    return evidence


def make_specialist(
    name: SpecialistName, model: BaseChatModel, tools: Sequence[BaseTool]
) -> Node[dict[str, Any]]:
    agent = create_agent(
        model,
        list(tools),
        system_prompt=prompts.SPECIALISTS[name],
        response_format=ToolStrategy(SpecialistAssessment),
        name=name,
    )

    async def specialist(state: TriageState) -> dict[str, Any]:
        request = state.get("critic_request")
        instruction = request.instruction if request and name in request.specialists else None
        others = {k: v for k, v in state.get("findings", {}).items() if k != name}
        task = prompts.render_specialist_input(state["case"], others, instruction)
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(task)]},
                config={"recursion_limit": SPECIALIST_RECURSION_LIMIT},
            )
        except GraphRecursionError:
            finding = Finding(
                agent=name,
                risk_signal=RiskLevel.NONE,
                confidence=0.0,
                rationale=f"Stopped after {SPECIALIST_RECURSION_LIMIT} steps without an answer.",
            )
            return {"findings": {name: finding}}

        assessment = result["structured_response"]
        if not isinstance(assessment, SpecialistAssessment):
            assessment = SpecialistAssessment.model_validate(assessment)
        finding = Finding(
            agent=name,
            risk_signal=assessment.risk_signal,
            confidence=assessment.confidence,
            rationale=assessment.rationale,
            evidence=collect_evidence(result["messages"]),
        )
        return {"findings": {name: finding}}

    return specialist
