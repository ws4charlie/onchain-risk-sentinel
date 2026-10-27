"""A tiny configurable fixture world for counterparty and flow scenarios.

Independent of `tests.fakes` (fixed addresses) and of the eval generators: each test
builds exactly the addresses and transfers it needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count

from sentinel.core.ports import Ports
from sentinel.core.schemas import (
    AddressProfile,
    Chain,
    PatternDoc,
    SanctionHit,
    ScamReport,
    Transfer,
)

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
_ids = count(1)


def addr(tag: int) -> str:
    return f"0x{tag:040x}"


@dataclass
class World:
    profiles: dict[str, AddressProfile] = field(default_factory=dict)
    transfers: dict[str, list[Transfer]] = field(default_factory=dict)
    sanctioned: dict[str, SanctionHit] = field(default_factory=dict)
    reports: dict[str, list[ScamReport]] = field(default_factory=dict)

    def account(
        self,
        address: str,
        *,
        age_days: float,
        tx_count: int = 10,
        contract: bool = False,
        labels: list[str] | None = None,
    ) -> str:
        self.profiles[address] = AddressProfile(
            chain=Chain.ETHEREUM,
            address=address,
            is_contract=contract,
            balance_wei=10**18,
            tx_count=tx_count,
            first_seen=NOW - timedelta(days=age_days),
            last_seen=NOW - timedelta(hours=1),
            labels=labels or [],
        )
        return address

    def send(self, frm: str, to: str, amount: float, days_ago: float, asset: str = "ETH") -> None:
        n = next(_ids)
        transfer = Transfer(
            chain=Chain.ETHEREUM,
            tx_hash=f"0x{n:064x}",
            block_number=21_000_000 + n,
            timestamp=NOW - timedelta(days=days_ago),
            from_address=frm,
            to_address=to,
            asset=asset,
            amount=amount,
        )
        for side in {frm, to}:
            self.transfers.setdefault(side, []).insert(0, transfer)

    def sanction(self, address: str) -> None:
        self.sanctioned[address] = SanctionHit(
            address=address, list_name="OFAC SDN", entity_name="Test Entity"
        )

    def ports(self) -> Ports:
        return Ports(
            chain_data=_Chain(self),
            sanctions=_Sanctions(self),
            scam_intel=_Intel(self),
            patterns=_Patterns(),
        )


class _Chain:
    def __init__(self, world: World) -> None:
        self.world = world

    async def get_address_profile(self, chain: Chain, address: str) -> AddressProfile:
        return self.world.profiles.get(
            address,
            AddressProfile(
                chain=chain, address=address, is_contract=False, balance_wei=0, tx_count=0
            ),
        )

    async def get_recent_transfers(
        self, chain: Chain, address: str, limit: int = 50
    ) -> list[Transfer]:
        history = sorted(self.world.transfers.get(address, []), key=lambda t: t.timestamp)
        return list(reversed(history))[:limit]


class _Sanctions:
    def __init__(self, world: World) -> None:
        self.world = world

    async def check(self, address: str) -> SanctionHit | None:
        return self.world.sanctioned.get(address)


class _Intel:
    def __init__(self, world: World) -> None:
        self.world = world

    async def lookup(self, address: str) -> list[ScamReport]:
        return self.world.reports.get(address, [])


class _Patterns:
    async def search(self, query: str, k: int = 4) -> list[PatternDoc]:
        return []


DEST = addr(0xD)


def one_hop_sanctioned(share_eth: float = 60.0) -> World:
    w = World()
    w.account(DEST, age_days=50)
    listed = w.account(addr(0x51), age_days=600)
    w.sanction(listed)
    peer = w.account(addr(0x91), age_days=900)
    w.send(listed, DEST, share_eth, days_ago=3)
    w.send(peer, DEST, 2.0, days_ago=20)
    return w


def mixer_funded(label: str = "contract:TornadoCash_Eth") -> World:
    w = World()
    w.account(DEST, age_days=20)
    mixer = w.account(addr(0x77), age_days=1500, contract=True, labels=[label])
    peer = w.account(addr(0x92), age_days=800)
    w.send(mixer, DEST, 100.0, days_ago=4)
    w.send(DEST, peer, 30.0, days_ago=1)
    return w


def peel_chain() -> World:
    w = World()
    w.account(DEST, age_days=3)
    source = w.account(addr(0x5), age_days=4)
    w.send(source, DEST, 200.0, days_ago=2)
    for hop, amount in enumerate([30.0, 25.0, 20.0, 16.0, 13.0]):
        out = w.account(addr(0x100 + hop), age_days=0.5, tx_count=1)
        w.send(DEST, out, amount, days_ago=1.5 - hop * 0.2)
    return w


def fresh_funded() -> World:
    w = World()
    w.account(DEST, age_days=1)
    for i in range(3):
        funder = w.account(addr(0x200 + i), age_days=2, tx_count=3)
        w.send(funder, DEST, 40.0, days_ago=0.5)
    return w
