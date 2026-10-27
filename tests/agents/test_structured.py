"""ask_structured: one corrective retry, then a clean error."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from sentinel.agents.structured import StructuredOutputError, ask_structured
from sentinel.agents.supervisor import RoutingDecision
from tests.agents.fake_llm import ScriptedChatModel, answer, tool_call

BAD = tool_call("RoutingDecision", {"specialists": ["no_such_agent"], "reason": "x"})


def _script(*replies: AIMessage) -> ScriptedChatModel:
    queue = list(replies)

    def policy(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
        return queue.pop(0)

    return ScriptedChatModel(policy=policy)


async def test_retry_succeeds_on_second_try() -> None:
    good = answer(RoutingDecision, specialists=["sanctions"], reason="ok")
    model = _script(BAD, good)

    decision = await ask_structured(model, RoutingDecision, [HumanMessage("route")])

    assert decision.specialists == ["sanctions"]
    second_call: list[Any] = model.calls[1]
    assert "not a valid RoutingDecision" in str(second_call[-1].content)


async def test_retry_gives_up_after_one() -> None:
    model = _script(BAD, AIMessage(content="no tool call"), BAD)

    with pytest.raises(StructuredOutputError, match="after 2 attempts"):
        await ask_structured(model, RoutingDecision, [HumanMessage("route")])
    assert len(model.calls) == 2
