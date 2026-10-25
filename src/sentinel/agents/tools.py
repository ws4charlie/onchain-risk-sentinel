"""Minimal LangChain tools over the ports, grouped by specialist.

Each tool returns `(content, artifact)`: the content is what the model reads,
the artifact is the list of `Evidence` objects built from the same port data.
Specialists collect evidence from artifacts, never from model text, so a
model cannot invent evidence it did not retrieve.

Tools that need the case itself (not just an address the model passes) read it
from the agent's runtime context (`SpecialistContext`), which the specialist
node sets per run. The model cannot change it.

Evidence data must survive checkpointing: msgpack cannot hold integers outside
the signed 64-bit range (e.g. balances in wei), so `_json_safe` stores those as
decimal strings.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from sentinel.agents.state import SpecialistName
from sentinel.core.ports import Ports
from sentinel.core.schemas import Case, Chain, Evidence, normalize_address

ToolResult = tuple[str, list[Evidence]]

_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

# Look-alike rule: same first and last LOOKALIKE_CHARS hex characters, different address.
LOOKALIKE_CHARS = 4
KNOWN_ADDRESS_KEYS = ("recent_withdrawal_addresses", "address_book")


@dataclass(frozen=True)
class SpecialistContext:
    """Per-run context injected into specialist tools; not visible to the model."""

    case: Case


def _json_safe(value: Any) -> Any:
    """Return `value` with every int outside the signed 64-bit range as a decimal string."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    return value


def _result(evidence: Sequence[Evidence]) -> ToolResult:
    safe = [e.model_copy(update={"data": _json_safe(e.data)}) for e in evidence]
    content = json.dumps([e.model_dump(mode="json", exclude={"observed_at"}) for e in safe])
    return content, safe


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


def _known_addresses(context: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield (address, source key) for every valid address the account used or saved."""
    for key in KNOWN_ADDRESS_KEYS:
        entries = context.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            raw = entry.get("address") if isinstance(entry, dict) else entry
            if not isinstance(raw, str):
                continue
            try:
                yield normalize_address(raw), key
            except ValueError:
                continue


def _common_prefix(a: str, b: str) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[n] == b[n]:
        n += 1
    return n


def find_lookalikes(address: str, context: dict[str, Any]) -> list[Evidence]:
    """Compare `address` with the account's known addresses (address-poisoning check)."""
    target = address[2:]
    matches: list[Evidence] = []
    known = list(dict.fromkeys(_known_addresses(context)))
    for other, source in known:
        hex_other = other[2:]
        same_ends = (
            target[:LOOKALIKE_CHARS] == hex_other[:LOOKALIKE_CHARS]
            and target[-LOOKALIKE_CHARS:] == hex_other[-LOOKALIKE_CHARS:]
        )
        if other == address or not same_ends:
            continue
        prefix = _common_prefix(target, hex_other)
        suffix = _common_prefix(target[::-1], hex_other[::-1])
        matches.append(
            Evidence(
                source="account_context",
                kind="lookalike_match",
                summary=f"{address} mimics known address {other} from {source}: same first "
                f"{prefix} and last {suffix} hex characters, but a different address",
                data={
                    "address": address,
                    "known_address": other,
                    "known_source": source,
                    "rule_chars": LOOKALIKE_CHARS,
                    "common_prefix_len": prefix,
                    "common_suffix_len": suffix,
                },
            )
        )
    if matches:
        return matches
    exact = any(other == address for other, _ in known)
    return [
        Evidence(
            source="account_context",
            kind="lookalike_clear",
            summary=f"{address} does not mimic any of {len(known)} known account addresses"
            + (" (it is one of them)" if exact else ""),
            data={"address": address, "known_count": len(known), "exact_known_match": exact},
        )
    ]


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

    @tool(response_format="content_and_artifact")
    async def compare_with_known_addresses(
        address: str, runtime: ToolRuntime[SpecialistContext]
    ) -> ToolResult:
        """Check whether an address imitates (address poisoning) one the account used before."""
        try:
            address = normalize_address(address)
        except ValueError:
            return _bad_address(address)
        return _result(find_lookalikes(address, runtime.context.case.account_context))

    return {
        "chain_tracer": [get_address_profile, get_recent_transfers, compare_with_known_addresses],
        "sanctions": [check_sanctions],
        "scam_intel": [lookup_scam_reports],
        "pattern_analyst": [search_patterns],
    }
