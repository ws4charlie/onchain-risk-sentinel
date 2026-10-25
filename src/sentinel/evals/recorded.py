"""`RecordedPorts`: replay a case's snapshot through the four ports.

Chain data, sanctions and scam intel come from the snapshot. Pattern search
uses the real local knowledge base (`data/patterns/`) with the offline,
deterministic `HashingEmbeddings`, so it needs no key and returns the same
results on every machine. A lookup the snapshot does not cover raises
`UnrecordedLookupError`; the agent tools turn that into an error message, and
the miss is counted so the results show when a model wandered off the record.
"""

from __future__ import annotations

from pathlib import Path

from sentinel.core.ports import PatternKnowledgeBase, Ports
from sentinel.core.schemas import (
    AddressProfile,
    Chain,
    SanctionHit,
    ScamReport,
    Transfer,
    normalize_address,
)
from sentinel.evals.dataset import Snapshot
from sentinel.knowledge import HashingEmbeddings, VectorPatternKnowledgeBase


class UnrecordedLookupError(LookupError):
    """The snapshot has no recorded answer for this lookup."""


class _Replay:
    def __init__(self, snapshot: Snapshot, misses: list[str]) -> None:
        self._snapshot = snapshot
        self._misses = misses

    def _miss(self, port: str, address: str) -> UnrecordedLookupError:
        self._misses.append(f"{port}:{address}")
        return UnrecordedLookupError(f"no recorded {port} data for {address}")

    @staticmethod
    def _key(address: str) -> str:
        try:
            return normalize_address(address)
        except ValueError:
            return address


class RecordedChainData(_Replay):
    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        profile = self._snapshot.profiles.get(self._key(address))
        if profile is None:
            raise self._miss("profile", address)
        return profile

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        transfers = self._snapshot.transfers.get(self._key(address))
        if transfers is None:
            raise self._miss("transfers", address)
        return transfers[:limit]


class RecordedSanctions(_Replay):
    async def check(self, address: str) -> SanctionHit | None:
        key = self._key(address)
        if key not in self._snapshot.sanctions:
            raise self._miss("sanctions", address)
        return self._snapshot.sanctions[key]


class RecordedScamIntel(_Replay):
    async def lookup(self, address: str) -> list[ScamReport]:
        reports = self._snapshot.scam_reports.get(self._key(address))
        if reports is None:
            raise self._miss("scam_reports", address)
        return reports


def offline_pattern_kb(index_dir: Path | None = None) -> VectorPatternKnowledgeBase:
    """The real pattern docs with deterministic offline embeddings (no API key)."""
    return VectorPatternKnowledgeBase.from_directory(HashingEmbeddings(), index_dir=index_dir)


class RecordedPorts(Ports):
    """Ports for one case. `misses` lists lookups the snapshot could not answer."""

    def __init__(self, snapshot: Snapshot, patterns: PatternKnowledgeBase) -> None:
        self.misses: list[str] = []
        super().__init__(
            chain_data=RecordedChainData(snapshot, self.misses),
            sanctions=RecordedSanctions(snapshot, self.misses),
            scam_intel=RecordedScamIntel(snapshot, self.misses),
            patterns=patterns,
        )
