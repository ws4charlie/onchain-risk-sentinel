"""Minimal LangChain tools over the ports, grouped by specialist.

Each tool returns `(content, artifact)`: the content is what the model reads,
the artifact is the list of `Evidence` objects built from the same port data.
Specialists collect evidence from artifacts, never from model text, so a
model cannot invent evidence it did not retrieve.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable, Sequence

from langchain_core.tools import BaseTool, tool

from sentinel.agents.state import SpecialistName
from sentinel.core.ports import Ports
from sentinel.core.schemas import Chain, Evidence, normalize_address

ToolResult = tuple[str, list[Evidence]]


def _result(evidence: Sequence[Evidence]) -> ToolResult:
    content = json.dumps([e.model_dump(mode="json", exclude={"observed_at"}) for e in evidence])
    return content, list(evidence)


def _bad_address(address: str) -> ToolResult:
    return f"Error: {address!r} is not a valid EVM address.", []


def _source_errors[**P](
    fn: Callable[P, Awaitable[ToolResult]],
) -> Callable[P, Awaitable[ToolResult]]:
    """Turn a failing data source into an error the model reads, with no evidence."""

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> ToolResult:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            return f"Error: data source failed ({type(exc).__name__}: {exc}).", []

    return wrapper


def build_tools(ports: Ports) -> dict[SpecialistName, list[BaseTool]]:
    """Return each specialist's own tools. No specialist sees another's tools."""

    @tool(response_format="content_and_artifact")
    @_source_errors
    async def get_address_profile(address: str) -> ToolResult:
        """Get age, activity, balance, contract status and labels of an Ethereum address."""
        try:
            address = normalize_address(address)
        except ValueError:
            return _bad_address(address)
        p = await ports.chain_data.get_address_profile(Chain.ETHEREUM, address)
        first = p.first_seen.date().isoformat() if p.first_seen else "never"
        summary = (
            f"{'Contract' if p.is_contract else 'EOA'} {address}: {p.tx_count} txs, "
            f"first seen {first}, balance {p.balance_wei} wei"
            + (f", labels {', '.join(p.labels)}" if p.labels else "")
        )
        return _result(
            [
                Evidence(
                    source="chain_data",
                    kind="address_profile",
                    summary=summary,
                    data=p.model_dump(mode="json"),
                )
            ]
        )

    @tool(response_format="content_and_artifact")
    @_source_errors
    async def get_recent_transfers(address: str, limit: int = 20) -> ToolResult:
        """Get the most recent transfers in or out of an Ethereum address, newest first."""
        try:
            address = normalize_address(address)
        except ValueError:
            return _bad_address(address)
        transfers = await ports.chain_data.get_recent_transfers(Chain.ETHEREUM, address, limit)
        zero = sum(1 for t in transfers if t.amount == 0)
        summary = f"{len(transfers)} recent transfers for {address}, {zero} of them zero-value"
        return _result(
            [
                Evidence(
                    source="chain_data",
                    kind="recent_transfers",
                    summary=summary,
                    data={
                        "zero_value_count": zero,
                        "transfers": [t.model_dump(mode="json") for t in transfers],
                    },
                )
            ]
        )

    @tool(response_format="content_and_artifact")
    @_source_errors
    async def check_sanctions(address: str) -> ToolResult:
        """Check whether an address is on a sanctions list."""
        try:
            address = normalize_address(address)
        except ValueError:
            return _bad_address(address)
        hit = await ports.sanctions.check(address)
        if hit is None:
            evidence = Evidence(
                source="sanctions",
                kind="sanctions_clear",
                summary=f"{address} is not on any screened sanctions list",
                data={"address": address},
            )
        else:
            evidence = Evidence(
                source=hit.list_name,
                kind="sanctions_hit",
                summary=f"{address} is listed on {hit.list_name} as {hit.entity_name}"
                + (f" (program {hit.program})" if hit.program else ""),
                data=hit.model_dump(mode="json"),
                url=hit.source_url,
            )
        return _result([evidence])

    @tool(response_format="content_and_artifact")
    @_source_errors
    async def lookup_scam_reports(address: str) -> ToolResult:
        """Look up public scam, phishing and drainer reports for an address."""
        try:
            address = normalize_address(address)
        except ValueError:
            return _bad_address(address)
        reports = await ports.scam_intel.lookup(address)
        if not reports:
            return _result(
                [
                    Evidence(
                        source="scam_intel",
                        kind="scam_intel_clear",
                        summary=f"No public scam reports for {address}",
                        data={"address": address},
                    )
                ]
            )
        return _result(
            [
                Evidence(
                    source=r.source,
                    kind="scam_report",
                    summary=f"{address} reported as {r.category}: {r.description}".rstrip(": "),
                    data=r.model_dump(mode="json"),
                    url=r.source_url,
                )
                for r in reports
            ]
        )

    @tool(response_format="content_and_artifact")
    @_source_errors
    async def search_patterns(query: str) -> ToolResult:
        """Search the fraud-pattern knowledge base with a description of observed behavior."""
        docs = await ports.patterns.search(query, k=3)
        return _result(
            [
                Evidence(
                    source="pattern_kb",
                    kind="pattern_match",
                    summary=f"Pattern '{d.title}' (relevance {d.score:.2f}): {d.content}",
                    data=d.model_dump(mode="json"),
                )
                for d in docs
            ]
        )

    return {
        "chain_tracer": [get_address_profile, get_recent_transfers],
        "sanctions": [check_sanctions],
        "scam_intel": [lookup_scam_reports],
        "pattern_analyst": [search_patterns],
    }
