"""Token usage and cost tracking as a LangChain callback handler.

Attach one `UsageCallbackHandler` per run (e.g. `graph.with_config(callbacks=[h])`
or through the runtime's default callbacks). It aggregates `usage_metadata` from
every chat model call, keyed by model and by the top-level graph node the call
ran in. Nodes map to roles (supervisor, specialist, scorer, critic), so cost can
be reported per role even when several roles share one model.

The node is the first segment of LangGraph's `langgraph_checkpoint_ns`, so calls
inside a specialist's own ReAct subgraph are attributed to that specialist.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import BaseModel, ConfigDict, Field

from sentinel.agents.state import SPECIALISTS
from sentinel.observability.pricing import DEFAULT_PRICES, PriceTable

UNKNOWN = "unknown"

NODE_ROLES: dict[str, str] = {
    "supervisor": "supervisor",
    "risk_scorer": "scorer",
    "critic": "critic",
    **{name: "specialist" for name in SPECIALISTS},
}


def role_for_node(node: str) -> str:
    return NODE_ROLES.get(node, UNKNOWN)


class TokenUsage(BaseModel):
    """Token counts for one (model, node) pair, or any aggregate of them."""

    model_config = ConfigDict(extra="forbid")

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = Field(default=0.0, description="None when a model has no price.")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        cost = (
            None
            if self.cost_usd is None or other.cost_usd is None
            else self.cost_usd + other.cost_usd
        )
        return TokenUsage(
            calls=self.calls + other.calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=cost,
        )


class UsageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    node: str
    role: str
    usage: TokenUsage


def sum_usage(items: Iterable[TokenUsage]) -> TokenUsage:
    total = TokenUsage()
    for item in items:
        total = total + item
    return total


def _model_name(metadata: Mapping[str, Any], invocation: Mapping[str, Any]) -> str:
    provider = metadata.get("ls_provider")
    name = metadata.get("ls_model_name") or invocation.get("model") or invocation.get("model_name")
    if not name:
        return UNKNOWN
    return f"{provider}:{name}" if provider else str(name)


def _node_name(metadata: Mapping[str, Any]) -> str:
    namespace = metadata.get("langgraph_checkpoint_ns")
    if isinstance(namespace, str) and namespace:
        return namespace.split("|", 1)[0].split(":", 1)[0]
    node = metadata.get("langgraph_node")
    return str(node) if node else UNKNOWN


def _usage_from_result(response: LLMResult) -> tuple[int, int, int]:
    """Sum (input, output, cached input) tokens over all generations of one call."""
    total_in = total_out = total_cached = 0
    found = False
    for generations in response.generations:
        for generation in generations:
            if not isinstance(generation, ChatGeneration):
                continue
            usage = getattr(generation.message, "usage_metadata", None)
            if not usage:
                continue
            found = True
            total_in += int(usage.get("input_tokens", 0))
            total_out += int(usage.get("output_tokens", 0))
            details = usage.get("input_token_details") or {}
            total_cached += int(details.get("cache_read", 0) or 0)
    if not found and response.llm_output:
        # Older integrations report usage only in llm_output["token_usage"].
        raw = response.llm_output.get("token_usage") or {}
        total_in = int(raw.get("prompt_tokens", 0))
        total_out = int(raw.get("completion_tokens", 0))
    return total_in, total_out, total_cached


class UsageCallbackHandler(BaseCallbackHandler):
    """Aggregates token usage per model and per graph node. Thread-safe."""

    run_inline = True
    raise_error = False

    def __init__(self, prices: PriceTable = DEFAULT_PRICES) -> None:
        self._prices = prices
        self._lock = threading.Lock()
        self._pending: dict[UUID, tuple[str, str]] = {}
        self._totals: dict[tuple[str, str], TokenUsage] = {}

    def _start(self, run_id: UUID, metadata: Mapping[str, Any] | None, kwargs: Any) -> None:
        metadata = metadata or {}
        invocation = kwargs.get("invocation_params") or {}
        with self._lock:
            self._pending[run_id] = (_model_name(metadata, invocation), _node_name(metadata))

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(run_id, metadata, kwargs)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._start(run_id, metadata, kwargs)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            model, node = self._pending.pop(run_id, (UNKNOWN, UNKNOWN))
        input_tokens, output_tokens, cached = _usage_from_result(response)
        price = self._prices.lookup(model)
        usage = TokenUsage(
            calls=1,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached,
            cost_usd=price.cost(input_tokens, output_tokens, cached) if price else None,
        )
        with self._lock:
            key = (model, node)
            self._totals[key] = self._totals.get(key, TokenUsage()) + usage

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        with self._lock:
            self._pending.pop(run_id, None)

    def records(self) -> list[UsageRecord]:
        with self._lock:
            items = sorted(self._totals.items())
        return [
            UsageRecord(model=model, node=node, role=role_for_node(node), usage=usage.model_copy())
            for (model, node), usage in items
        ]

    def by_role(self) -> dict[str, TokenUsage]:
        return self._group(lambda r: r.role)

    def by_model(self) -> dict[str, TokenUsage]:
        return self._group(lambda r: r.model)

    def total(self) -> TokenUsage:
        return sum_usage(r.usage for r in self.records())

    def _group(self, key: Any) -> dict[str, TokenUsage]:
        groups: dict[str, TokenUsage] = {}
        for record in self.records():
            name = key(record)
            groups[name] = groups.get(name, TokenUsage()) + record.usage
        return groups
