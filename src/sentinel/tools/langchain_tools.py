"""LangChain tools over the ports, one factory per port.

Each tool takes a Pydantic argument schema, normalizes addresses before calling the
port, and returns JSON serialized from the `sentinel.core.schemas` models, so agents
can quote exact values as evidence. Bad input and adapter failures become a
`ToolException`, which LangChain hands back to the model as the tool result.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from pydantic import BaseModel, Field

from sentinel.core.ports import (
    ChainDataSource,
    PatternKnowledgeBase,
    Ports,
    SanctionsSource,
    ScamIntelSource,
)
from sentinel.core.schemas import Chain, normalize_address

_ADDRESS_DESC = "Ethereum address, 0x followed by 40 hex characters (any letter case)."


class AddressArgs(BaseModel):
    address: str = Field(description=_ADDRESS_DESC)


class TransfersArgs(BaseModel):
    address: str = Field(description=_ADDRESS_DESC)
    limit: int = Field(default=25, ge=1, le=200, description="Max transfers to return.")


class PatternSearchArgs(BaseModel):
    query: str = Field(
        min_length=3,
        description="Natural-language description of the observed behavior, e.g. "
        "'fresh address received many zero-value token transfers from look-alike senders'.",
    )
    k: int = Field(default=4, ge=1, le=10, description="Number of patterns to return.")


def _to_json(value: BaseModel | Sequence[BaseModel] | None) -> str:
    if value is None:
        data: Any = None
    elif isinstance(value, BaseModel):
        data = value.model_dump(mode="json")
    else:
        data = [item.model_dump(mode="json") for item in value]
    return json.dumps(data, ensure_ascii=False)


def _address(value: str) -> str:
    try:
        return normalize_address(value)
    except ValueError as exc:
        raise ToolException(str(exc)) from exc


async def _guard(call: Callable[[], Awaitable[str]], what: str) -> str:
    try:
        return await call()
    except ToolException:
        raise
    except Exception as exc:  # adapter failures must reach the model, not crash the graph
        raise ToolException(f"{what} failed: {exc}") from exc


def _tool(
    name: str,
    description: str,
    args_schema: type[BaseModel],
    coroutine: Callable[..., Awaitable[str]],
) -> BaseTool:
    return StructuredTool.from_function(
        coroutine=coroutine,
        name=name,
        description=description,
        args_schema=args_schema,
        handle_tool_error=True,
    )


def chain_data_tools(source: ChainDataSource, chain: Chain = Chain.ETHEREUM) -> list[BaseTool]:
    async def get_address_profile(address: str) -> str:
        addr = _address(address)

        async def call() -> str:
            return _to_json(await source.get_address_profile(chain, addr))

        return await _guard(call, "get_address_profile")

    async def get_recent_transfers(address: str, limit: int = 25) -> str:
        addr = _address(address)

        async def call() -> str:
            return _to_json(await source.get_recent_transfers(chain, addr, limit))

        return await _guard(call, "get_recent_transfers")

    return [
        _tool(
            "get_address_profile",
            "Look up on-chain facts about an Ethereum address: whether it is a contract, "
            "balance in wei, number of transactions it has sent, first and last activity "
            "time, and labels. Use it to judge address age and activity level.",
            AddressArgs,
            get_address_profile,
        ),
        _tool(
            "get_recent_transfers",
            "List the most recent ETH, internal and ERC-20 transfers into or out of an "
            "Ethereum address, newest first, with counterparties, asset and amount. Use it "
            "to find counterparties, zero-value transfers, sudden inflows or peel chains.",
            TransfersArgs,
            get_recent_transfers,
        ),
    ]


def sanctions_tools(source: SanctionsSource) -> list[BaseTool]:
    async def check_sanctions(address: str) -> str:
        addr = _address(address)

        async def call() -> str:
            return _to_json(await source.check(addr))

        return await _guard(call, "check_sanctions")

    return [
        _tool(
            "check_sanctions",
            "Check whether an Ethereum address is on the OFAC SDN sanctions list. Returns the "
            "matching entry (entity name, program, list) or null when there is no match. "
            "Check the destination and any notable counterparties.",
            AddressArgs,
            check_sanctions,
        )
    ]


def scam_intel_tools(source: ScamIntelSource) -> list[BaseTool]:
    async def lookup_scam_reports(address: str) -> str:
        addr = _address(address)

        async def call() -> str:
            return _to_json(await source.lookup(addr))

        return await _guard(call, "lookup_scam_reports")

    return [
        _tool(
            "lookup_scam_reports",
            "Look up public scam and phishing reports for an Ethereum address (drainers, "
            "phishing, exploit contracts and their deployers). Returns a list of reports "
            "with category and source; an empty list means no public report, not proof "
            "the address is safe.",
            AddressArgs,
            lookup_scam_reports,
        )
    ]


def pattern_tools(source: PatternKnowledgeBase) -> list[BaseTool]:
    async def search_fraud_patterns(query: str, k: int = 4) -> str:
        async def call() -> str:
            return _to_json(await source.search(query, k))

        return await _guard(call, "search_fraud_patterns")

    return [
        _tool(
            "search_fraud_patterns",
            "Search a knowledge base of documented on-chain fraud and risk patterns (address "
            "poisoning, approval drainers, mixers, peel chains, pig-butchering, ...). "
            "Describe what you observed; returns the best-matching patterns with their "
            "indicators and a relevance score (higher is closer).",
            PatternSearchArgs,
            search_fraud_patterns,
        )
    ]


def tools_by_specialist(ports: Ports, chain: Chain = Chain.ETHEREUM) -> dict[str, list[BaseTool]]:
    """Tools grouped by the specialist agent that should own them."""
    return {
        "chain_tracer": chain_data_tools(ports.chain_data, chain),
        "sanctions": sanctions_tools(ports.sanctions),
        "scam_intel": scam_intel_tools(ports.scam_intel),
        "pattern_analyst": pattern_tools(ports.patterns),
    }
