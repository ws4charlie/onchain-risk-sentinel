"""Ports: the interfaces between agents and data.

Agents depend only on these Protocols. Concrete adapters live in
`sentinel.tools` and `sentinel.knowledge`; test fakes live in `tests/fakes.py`.
All addresses passed in are already normalized (see `normalize_address`).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from sentinel.core.schemas import (
    AddressProfile,
    Chain,
    PatternDoc,
    SanctionHit,
    ScamReport,
    Transfer,
)


@runtime_checkable
class ChainDataSource(Protocol):
    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        """Return summary facts about an address."""
        ...

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        """Return the most recent transfers in or out of an address, newest first."""
        ...


@runtime_checkable
class SanctionsSource(Protocol):
    async def check(self, address: str) -> SanctionHit | None:
        """Return a hit if the address is on a sanctions list, else None."""
        ...


@runtime_checkable
class ScamIntelSource(Protocol):
    async def lookup(self, address: str) -> list[ScamReport]:
        """Return public scam/phishing reports for an address (empty if none)."""
        ...


@runtime_checkable
class PatternKnowledgeBase(Protocol):
    async def search(self, query: str, k: int = 4) -> list[PatternDoc]:
        """Return the k most relevant fraud-pattern documents, best first."""
        ...


class Ports:
    """Bundle of port implementations passed to the graph builder."""

    def __init__(
        self,
        chain_data: ChainDataSource,
        sanctions: SanctionsSource,
        scam_intel: ScamIntelSource,
        patterns: PatternKnowledgeBase,
    ) -> None:
        self.chain_data = chain_data
        self.sanctions = sanctions
        self.scam_intel = scam_intel
        self.patterns = patterns
