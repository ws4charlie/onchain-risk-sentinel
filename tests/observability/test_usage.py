"""Usage callback aggregation and pricing, with fake usage_metadata (no LLM calls)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from sentinel.observability import (
    DEFAULT_PRICES,
    ModelPrice,
    PriceTable,
    TokenUsage,
    UsageCallbackHandler,
    role_for_node,
)


def _message(inp: int, out: int, cached: int = 0) -> AIMessage:
    usage: dict[str, Any] = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    if cached:
        usage["input_token_details"] = {"cache_read": cached}
    return AIMessage(content="ok", usage_metadata=usage)  # type: ignore[arg-type]


def _call(handler: UsageCallbackHandler, model: str, namespace: str, message: AIMessage) -> None:
    provider, name = model.split(":", 1)
    run_id = uuid.uuid4()
    handler.on_chat_model_start(
        {},
        [[]],
        run_id=run_id,
        metadata={
            "ls_provider": provider,
            "ls_model_name": name,
            "langgraph_checkpoint_ns": namespace,
        },
    )
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)


def test_aggregates_per_model_node_and_role() -> None:
    handler = UsageCallbackHandler()
    _call(handler, "openai:gpt-5.4-mini", "supervisor:1", _message(1000, 100))
    _call(handler, "openai:gpt-5.4-mini", "supervisor:2", _message(2000, 200, cached=1000))
    _call(handler, "openai:gpt-5.4-mini", "chain_tracer:3|model:4", _message(500, 50))
    _call(handler, "openai:gpt-5.4-mini", "sanctions:5|model:6", _message(500, 50))
    _call(handler, "ollama:qwen3:8b", "critic:7", _message(300, 30))

    records = {(r.model, r.node): r for r in handler.records()}
    supervisor = records[("openai:gpt-5.4-mini", "supervisor")].usage
    assert supervisor.calls == 2
    assert supervisor.input_tokens == 3000
    assert supervisor.cached_input_tokens == 1000

    roles = handler.by_role()
    assert roles["specialist"].calls == 2
    assert roles["specialist"].input_tokens == 1000
    assert roles["critic"].cost_usd == 0.0

    # 2000 uncached + 1000 cached input, 300 output on the supervisor.
    expected_supervisor = (2000 * 0.75 + 1000 * 0.075 + 300 * 4.50) / 1e6
    assert supervisor.cost_usd == pytest.approx(expected_supervisor)

    total = handler.total()
    assert total.calls == 5
    assert total.total_tokens == 4300 + 430
    assert set(handler.by_model()) == {"openai:gpt-5.4-mini", "ollama:qwen3:8b"}


def test_unpriced_model_makes_cost_unknown() -> None:
    handler = UsageCallbackHandler()
    _call(handler, "openai:gpt-5.4-mini", "supervisor:1", _message(10, 1))
    _call(handler, "acme:unknown-model", "critic:2", _message(10, 1))
    assert handler.by_role()["supervisor"].cost_usd is not None
    assert handler.by_role()["critic"].cost_usd is None
    assert handler.total().cost_usd is None


def test_error_and_missing_usage_are_harmless() -> None:
    handler = UsageCallbackHandler()
    run_id = uuid.uuid4()
    handler.on_chat_model_start({}, [[]], run_id=run_id, metadata={})
    handler.on_llm_error(RuntimeError("boom"), run_id=run_id)
    _call(handler, "openai:gpt-5.4-mini", "critic:1", AIMessage(content="no usage"))
    assert handler.total().calls == 1
    assert handler.total().total_tokens == 0


async def test_collects_usage_from_a_real_callback_run() -> None:
    model = GenericFakeChatModel(messages=iter([_message(120, 30), _message(80, 20)]))
    handler = UsageCallbackHandler()
    config: Any = {"callbacks": [handler], "metadata": {"langgraph_node": "risk_scorer"}}
    await model.ainvoke("hello", config=config)
    await model.ainvoke("again", config=config)
    usage = handler.by_role()["scorer"]
    assert usage.calls == 2
    assert usage.input_tokens == 200
    assert usage.output_tokens == 50


def test_role_mapping() -> None:
    assert role_for_node("supervisor") == "supervisor"
    assert role_for_node("pattern_analyst") == "specialist"
    assert role_for_node("risk_scorer") == "scorer"
    assert role_for_node("something_else") == "unknown"


def test_price_lookup_and_cost() -> None:
    assert DEFAULT_PRICES.lookup("openai:gpt-5.4-nano") is not None
    assert DEFAULT_PRICES.lookup("gpt-5.4-nano") is not None  # bare name
    local = DEFAULT_PRICES.lookup("ollama:qwen3:8b")
    assert local is not None and local.cost(10**6, 10**6) == 0
    assert DEFAULT_PRICES.lookup("openai:not-a-model") is None
    price = DEFAULT_PRICES.lookup("openai:gpt-5.4-mini")
    assert price is not None
    assert price.cost(1_000_000, 1_000_000) == pytest.approx(5.25)


def test_price_table_from_file(tmp_path: Any) -> None:
    table = PriceTable(
        prices={
            "x:model": ModelPrice(
                input_per_mtok=1, output_per_mtok=2, source="test", checked_on="2026-10-25"
            )
        }
    )
    path = tmp_path / "prices.json"
    path.write_text(table.model_dump_json())
    assert PriceTable.from_file(path) == table


def test_token_usage_addition() -> None:
    a = TokenUsage(calls=1, input_tokens=5, output_tokens=1, cost_usd=0.5)
    b = TokenUsage(calls=2, input_tokens=5, output_tokens=2, cost_usd=0.25)
    assert (a + b).model_dump() == {
        "calls": 3,
        "input_tokens": 10,
        "output_tokens": 3,
        "cached_input_tokens": 0,
        "cost_usd": 0.75,
    }
