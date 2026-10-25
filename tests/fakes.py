"""In-memory fake ports with a small fixture world, for tests that must not hit the network.

Addresses are synthetic. Each one represents a scenario the graph must handle:

- CLEAN: an old, active address with ordinary transfers.
- SANCTIONED: on the sanctions list.
- DRAINER: reported as an approval drainer.
- POISONING_LOOKALIKE: a fresh address that mimics CLEAN's first and last characters
  and received only zero-value transfers (address-poisoning pattern).
- FRESH: brand new, no history, no reports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sentinel.core.ports import Ports
from sentinel.core.schemas import (
    AddressProfile,
    Chain,
    PatternDoc,
    SanctionHit,
    ScamReport,
    Transfer,
)

CLEAN = "0x1111111111111111111111111111111111111a2b"
SANCTIONED = "0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
DRAINER = "0xd4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1"
POISONING_LOOKALIKE = "0x1111999999999999999999999999999999991a2b"
FRESH = "0xf3e5f3e5f3e5f3e5f3e5f3e5f3e5f3e5f3e5f3e5"
COUNTERPARTY = "0xc0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"

_NOW = datetime(2026, 10, 1, tzinfo=UTC)


def _transfer(n: int, frm: str, to: str, amount: float, days_ago: int) -> Transfer:
    return Transfer(
        chain=Chain.ETHEREUM,
        tx_hash=f"0x{n:064x}",
        block_number=21_000_000 + n,
        timestamp=_NOW - timedelta(days=days_ago),
        from_address=frm,
        to_address=to,
        asset="ETH",
        amount=amount,
    )


PROFILES: dict[str, AddressProfile] = {
    CLEAN: AddressProfile(
        chain=Chain.ETHEREUM,
        address=CLEAN,
        is_contract=False,
        balance_wei=3 * 10**18,
        tx_count=412,
        first_seen=_NOW - timedelta(days=1400),
        last_seen=_NOW - timedelta(days=2),
    ),
    SANCTIONED: AddressProfile(
        chain=Chain.ETHEREUM,
        address=SANCTIONED,
        is_contract=False,
        balance_wei=10**17,
        tx_count=88,
        first_seen=_NOW - timedelta(days=900),
        last_seen=_NOW - timedelta(days=30),
    ),
    DRAINER: AddressProfile(
        chain=Chain.ETHEREUM,
        address=DRAINER,
        is_contract=True,
        balance_wei=0,
        tx_count=1530,
        first_seen=_NOW - timedelta(days=60),
        last_seen=_NOW - timedelta(days=1),
    ),
    POISONING_LOOKALIKE: AddressProfile(
        chain=Chain.ETHEREUM,
        address=POISONING_LOOKALIKE,
        is_contract=False,
        balance_wei=0,
        tx_count=3,
        first_seen=_NOW - timedelta(days=1),
        last_seen=_NOW - timedelta(hours=3),
    ),
    FRESH: AddressProfile(
        chain=Chain.ETHEREUM,
        address=FRESH,
        is_contract=False,
        balance_wei=0,
        tx_count=0,
    ),
}

TRANSFERS: dict[str, list[Transfer]] = {
    CLEAN: [
        _transfer(1, COUNTERPARTY, CLEAN, 1.2, 2),
        _transfer(2, CLEAN, COUNTERPARTY, 0.4, 10),
    ],
    SANCTIONED: [_transfer(3, SANCTIONED, COUNTERPARTY, 5.0, 30)],
    DRAINER: [_transfer(4, COUNTERPARTY, DRAINER, 0.9, 1)],
    POISONING_LOOKALIKE: [
        _transfer(5, POISONING_LOOKALIKE, CLEAN, 0.0, 0),
        _transfer(6, POISONING_LOOKALIKE, COUNTERPARTY, 0.0, 0),
    ],
    FRESH: [],
}

SANCTIONS: dict[str, SanctionHit] = {
    SANCTIONED: SanctionHit(
        address=SANCTIONED,
        list_name="OFAC SDN",
        entity_name="Example Sanctioned Entity",
        program="CYBER2",
    )
}

SCAM_REPORTS: dict[str, list[ScamReport]] = {
    DRAINER: [
        ScamReport(
            address=DRAINER,
            category="drainer",
            source="fixture",
            description="Collects token approvals via a fake airdrop site.",
        )
    ]
}

PATTERNS: list[PatternDoc] = [
    PatternDoc(
        doc_id="address-poisoning",
        title="Address poisoning",
        content="Attacker sends zero-value transfers from a look-alike address so the "
        "victim copies it from history.",
        indicators=["zero-value transfers", "look-alike prefix/suffix", "fresh address"],
        score=0.0,
    ),
    PatternDoc(
        doc_id="approval-drainer",
        title="Approval drainer",
        content="Phishing site tricks users into signing token approvals, then a contract "
        "sweeps funds.",
        indicators=["contract recipient", "many inbound approvals", "recent deployment"],
        score=0.0,
    ),
    PatternDoc(
        doc_id="sanctioned-exposure",
        title="Sanctioned entity exposure",
        content="Funds sent directly or within one hop of a sanctioned address.",
        indicators=["sanctions list hit", "one-hop counterparty is sanctioned"],
        score=0.0,
    ),
]


class FakeChainData:
    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        return PROFILES.get(
            address,
            AddressProfile(
                chain=chain, address=address, is_contract=False, balance_wei=0, tx_count=0
            ),
        )

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        return TRANSFERS.get(address, [])[:limit]


class FakeSanctions:
    async def check(self, address: str) -> SanctionHit | None:
        return SANCTIONS.get(address)


class FakeScamIntel:
    async def lookup(self, address: str) -> list[ScamReport]:
        return SCAM_REPORTS.get(address, [])


class FakePatterns:
    """Keyword-overlap search; good enough to make results deterministic."""

    async def search(self, query: str, k: int = 4) -> list[PatternDoc]:
        words = set(query.lower().split())
        scored = []
        for doc in PATTERNS:
            text = f"{doc.title} {doc.content} {' '.join(doc.indicators)}".lower()
            overlap = sum(1 for w in words if w in text)
            scored.append(doc.model_copy(update={"score": float(overlap)}))
        scored.sort(key=lambda d: d.score, reverse=True)
        return scored[:k]


def fake_ports() -> Ports:
    return Ports(
        chain_data=FakeChainData(),
        sanctions=FakeSanctions(),
        scam_intel=FakeScamIntel(),
        patterns=FakePatterns(),
    )
