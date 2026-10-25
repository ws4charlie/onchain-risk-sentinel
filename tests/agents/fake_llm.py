"""A scripted chat model for graph tests: no network, deterministic.

`ScriptedChatModel` hands every call to a `policy` function together with the
names of the tools bound to it. Structured output (`with_structured_output`)
and `create_agent`'s ToolStrategy both bind the schema as a tool, so a policy
answers them by emitting a tool call named after the schema.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field

Policy = Callable[[list[BaseMessage], list[str]], AIMessage]


def tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    call_id = f"call_{uuid.uuid4().hex[:8]}"
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def answer(schema: type[BaseModel], **fields: Any) -> AIMessage:
    """A structured answer: a tool call named after the schema."""
    return tool_call(schema.__name__, schema(**fields).model_dump(mode="json"))


class ScriptedChatModel(BaseChatModel):
    policy: Policy
    bound_tools: list[str] = Field(default_factory=list)
    calls: list[list[BaseMessage]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls.append(list(messages))
        message = self.policy(list(messages), self.bound_tools)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any
    ) -> Runnable[LanguageModelInput, AIMessage]:
        names = [convert_to_openai_tool(t)["function"]["name"] for t in tools]
        return self.model_copy(update={"bound_tools": names})


def queued(schema: type[BaseModel], answers: Iterable[dict[str, Any]], default: dict[str, Any]):
    """A policy that returns the queued structured answers in order, then `default` forever."""
    pending = deque(answers)

    def policy(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
        assert schema.__name__ in tools, f"expected {schema.__name__} to be bound, got {tools}"
        return answer(schema, **(pending.popleft() if pending else default))

    return policy


_ADDRESS = re.compile(r"0x[0-9a-f]{40}")


def specialist_policy(assessment: type[BaseModel]) -> Policy:
    """A rule-based ReAct specialist.

    First turn: call every bound data tool for the case address.
    Second turn: read the tool results and answer with an assessment.
    Stateless, so it is safe when specialists run in parallel.
    """

    def policy(messages: list[BaseMessage], tools: list[str]) -> AIMessage:
        data_tools = [t for t in tools if t != assessment.__name__]
        results = [m for m in messages if isinstance(m, ToolMessage)]
        if not results:
            task = next(m for m in messages if isinstance(m, HumanMessage))
            address = _ADDRESS.search(str(task.content))
            assert address is not None
            calls = []
            for name in data_tools:
                args = {"query": "zero-value transfers look-alike fresh address"}
                if name != "search_patterns":
                    args = {"address": address.group(0)}
                calls.append({"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:8]}"})
            return AIMessage(content="", tool_calls=calls)

        kinds = [e["kind"] for m in results for e in json.loads(str(m.content))]
        if "sanctions_hit" in kinds:
            signal, rationale = "critical", "The address is on a sanctions list."
        elif "scam_report" in kinds:
            signal, rationale = "high", "The address has a public scam report."
        else:
            signal, rationale = "none", f"Tool results show no risk: {', '.join(kinds)}."
        return answer(assessment, risk_signal=signal, confidence=0.9, rationale=rationale)

    return policy
