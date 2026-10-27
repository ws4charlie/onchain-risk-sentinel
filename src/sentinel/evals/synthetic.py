"""Builds the labeled eval dataset: synthetic scenarios plus real-address cases.

Synthetic cases get synthetic addresses and fully constructed snapshots, so the
whole scenario (including counterparties) is known. Real-address cases (OFAC
snapshot addresses, well-known protocol contracts) carry labels only; their
snapshots come from `python -m sentinel.evals.record`.

Deterministic: the same seed always produces the same files. Run
`python -m sentinel.evals.synthetic` to regenerate `cases.jsonl` and the
synthetic snapshots; recorded snapshots are left untouched.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from sentinel.core.schemas import (
    Action,
    AddressProfile,
    Chain,
    RiskLevel,
    SanctionHit,
    ScamReport,
    Transfer,
)
from sentinel.evals.dataset import (
    CASES_FILE,
    REFERENCE_TIME,
    SNAPSHOTS_DIR,
    EvalCase,
    Label,
    LabelSource,
    Snapshot,
    SnapshotOrigin,
    read_snapshot,
    write_cases,
    write_snapshot,
)

SEED = 20261020
WEI = 10**18
REFERENCE_BLOCK = 23_600_000  # approximate mainnet height at REFERENCE_TIME
SMOKE_SCENARIOS = (
    "sanctioned_direct",
    "drainer_report",
    "address_poisoning",
    "fresh_large_inflow",
    "benign_eoa",
    "benign_contract",
)

# Real-address cases. Labels only; snapshots are recorded with the real adapters.
# Sanctioned addresses come from data/sanctions/ofac_sdn_evm_addresses.json (public domain).
OFAC_ADDRESSES: tuple[tuple[str, str], ...] = (
    ("0x08723392ed15743cc38513c4925f5e6be5c17243", "DPRK3"),
    ("0x0330070fd38ec3bb94f58fa55d40368271e9e54a", "DPRK4"),
    ("0x95584c303fcd48af5c6b9873015f2ad0ca84eae3", "DPRK4"),
    ("0x12de548f79a50d2bd05481c8515c1ef5183666a9", "CYBER4"),
    ("0x1da5821544e25c636c1417ba96ade4cf6d2f9b5a", "CYBER2"),
    ("0x3cbded43efdaf0fc77b9c55f6fc9988fcc9b757d", "CYBER2"),
    ("0xd5ed34b52ac4ab84d8fa8a231a3218bbf01ed510", "CYBER3"),
    ("0x3ad9db589d201a710ed237c829c7860ba86510fc", "RUSSIA-EO14024"),
    ("0x21b8d56bda776bbe68655a16895afd96f5534fed", "SDGT"),
    ("0x530a64c0ce595026a4a556b703644228179e2d57", "ILLICIT-DRUGS-EO14059"),
)
# Long-lived, heavily used protocol contracts (token and DEX contracts, name service).
PROTOCOL_CONTRACTS: tuple[tuple[str, str], ...] = (
    ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "wrapped ether token contract"),
    ("0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", "major USD stablecoin token contract"),
    ("0x6b175474e89094c44da98b954eedeac495271d0f", "decentralized stablecoin token contract"),
    ("0x7a250d5630b4cf539739df2c5dacb4c659f2488d", "decentralized exchange router (v2)"),
    ("0xe592427a0aece92de3edee1f18e0157c05861564", "decentralized exchange router (v3)"),
    ("0x00000000000c2e074ec69a0dfb2997ba6c7d2e1e", "name-service registry contract"),
)


@dataclass
class Scenario:
    """A case under construction: input, label and the snapshot maps."""

    address: str
    label: Label
    amount: float
    asset: str = "ETH"
    account_context: dict[str, Any] = field(default_factory=dict)
    profiles: dict[str, AddressProfile] = field(default_factory=dict)
    transfers: dict[str, list[Transfer]] = field(default_factory=dict)
    sanctions: dict[str, SanctionHit | None] = field(default_factory=dict)
    scam_reports: dict[str, list[ScamReport]] = field(default_factory=dict)


class World:
    """Seeded generator of synthetic addresses, profiles and transfers."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)

    def address(self) -> str:
        return "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(40))

    def lookalike(self, of: str) -> str:
        """Same first 4 and last 4 hex characters, random middle (address poisoning)."""
        middle = self.address()[2 + 4 : 42 - 4]
        return of[:6] + middle + of[-4:]

    def tx_hash(self) -> str:
        return "0x" + "".join(self.rng.choice("0123456789abcdef") for _ in range(64))

    def profile(
        self,
        address: str,
        *,
        age_days: float | None,
        tx_count: int,
        balance_eth: float = 0.0,
        last_days: float = 0.5,
        contract: bool = False,
        labels: list[str] | None = None,
    ) -> AddressProfile:
        return AddressProfile(
            chain=Chain.ETHEREUM,
            address=address,
            is_contract=contract,
            balance_wei=int(balance_eth * WEI),
            tx_count=tx_count,
            first_seen=REFERENCE_TIME - timedelta(days=age_days) if age_days is not None else None,
            last_seen=REFERENCE_TIME - timedelta(days=last_days) if tx_count else None,
            labels=labels or [],
        )

    def transfer(
        self, frm: str, to: str, amount: float, days_ago: float, asset: str = "ETH"
    ) -> Transfer:
        when = REFERENCE_TIME - timedelta(days=days_ago)
        return Transfer(
            chain=Chain.ETHEREUM,
            tx_hash=self.tx_hash(),
            block_number=REFERENCE_BLOCK - int(days_ago * 7200),
            timestamp=when,
            from_address=frm,
            to_address=to,
            asset=asset,
            amount=round(amount, 6),
        )

    def counterparty(self, scenario: Scenario, address: str, profile: AddressProfile) -> None:
        """Register a clean counterparty on every port except transfers."""
        scenario.profiles[address] = profile
        scenario.sanctions.setdefault(address, None)
        scenario.scam_reports.setdefault(address, [])


def _label(action: Action, level: RiskLevel, scenario: str, rationale: str) -> Label:
    return Label(
        expected_action=action,
        min_risk_level=level,
        scenario=scenario,
        source=LabelSource.SYNTHETIC,
        rationale=rationale,
    )


def _finish(w: World, s: Scenario, history: list[Transfer]) -> Scenario:
    history.sort(key=lambda t: t.timestamp, reverse=True)
    s.transfers[s.address] = history
    s.sanctions.setdefault(s.address, None)
    s.scam_reports.setdefault(s.address, [])
    return s


def _ordinary_peers(w: World, s: Scenario, n: int) -> list[str]:
    peers = []
    for _ in range(n):
        peer = w.address()
        age = w.rng.uniform(200, 2500)
        w.counterparty(s, peer, w.profile(peer, age_days=age, tx_count=w.rng.randint(30, 3000)))
        peers.append(peer)
    return peers


# --- scenario builders ---------------------------------------------------------------------


def sanctioned_direct(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.BLOCK,
            RiskLevel.CRITICAL,
            "sanctioned_direct",
            "The destination itself is on the sanctions list; policy requires a block.",
        ),
        amount=round(w.rng.uniform(0.5, 40), 3),
    )
    s.profiles[addr] = w.profile(
        addr, age_days=w.rng.uniform(100, 1500), tx_count=w.rng.randint(20, 900), balance_eth=1.2
    )
    peers = _ordinary_peers(w, s, 3)
    history = [w.transfer(p, addr, w.rng.uniform(0.5, 30), w.rng.uniform(1, 90)) for p in peers]
    s.sanctions[addr] = SanctionHit(
        address=addr,
        list_name="OFAC SDN",
        entity_name=f"SYNTHETIC SANCTIONED ENTITY {i + 1:02d}",
        program=w.rng.choice(["CYBER2", "CYBER4", "DPRK3", "SDGT"]),
    )
    return _finish(w, s, history)


def drainer_report(w: World, i: int) -> Scenario:
    addr = w.address()
    category = "drainer" if i % 2 == 0 else "phishing"
    s = Scenario(
        addr,
        _label(
            Action.BLOCK,
            RiskLevel.HIGH,
            "drainer_report",
            f"A public scam feed reports the destination as {category}; funds sent there "
            "are lost to the operator.",
        ),
        amount=round(w.rng.uniform(0.2, 15), 3),
    )
    contract = i % 3 == 0
    s.profiles[addr] = w.profile(
        addr,
        age_days=w.rng.uniform(5, 120),
        tx_count=w.rng.randint(150, 4000),
        contract=contract,
    )
    victims = _ordinary_peers(w, s, 6)
    history = [w.transfer(v, addr, w.rng.uniform(0.05, 4), w.rng.uniform(0, 10)) for v in victims]
    sink = w.address()
    w.counterparty(s, sink, w.profile(sink, age_days=w.rng.uniform(1, 30), tx_count=12))
    history.append(w.transfer(addr, sink, w.rng.uniform(5, 20), w.rng.uniform(0, 2)))
    s.scam_reports[addr] = [
        ScamReport(
            address=addr,
            category=category,
            source="synthetic-feed",
            description="Collects wallet approvals through a fake claim page and sweeps funds."
            if category == "drainer"
            else "Linked to a phishing site impersonating a wallet support desk.",
        )
    ]
    return _finish(w, s, history)


def address_poisoning(w: World, i: int) -> Scenario:
    twin = w.address()
    addr = w.lookalike(twin)
    reported = i < 3
    s = Scenario(
        addr,
        _label(
            Action.BLOCK,
            RiskLevel.HIGH,
            "address_poisoning",
            "The destination mimics the first and last characters of an address the account "
            "used before, is days old and only sent zero-value transfers: an address-poisoning "
            "look-alike.",
        ),
        amount=round(w.rng.uniform(1, 60), 2),
        asset="USDT" if i % 2 else "ETH",
        account_context={
            "account_age_days": w.rng.randint(300, 2000),
            "recent_withdrawal_addresses": [twin],
        },
    )
    s.profiles[addr] = w.profile(
        addr, age_days=w.rng.uniform(0.5, 4), tx_count=w.rng.randint(3, 25), last_days=0.1
    )
    w.counterparty(s, twin, w.profile(twin, age_days=w.rng.uniform(300, 2000), tx_count=800))
    victims = _ordinary_peers(w, s, 4)
    history = [
        w.transfer(addr, v, 0.0, w.rng.uniform(0, 3), asset="USDT") for v in [twin, *victims]
    ]
    funder = w.address()
    w.counterparty(s, funder, w.profile(funder, age_days=2, tx_count=40))
    history.append(w.transfer(funder, addr, 0.02, 3.5))
    if reported:
        s.scam_reports[addr] = [
            ScamReport(
                address=addr,
                category="address_poisoning",
                source="synthetic-feed",
                description="Sends zero-value token transfers from look-alike addresses.",
            )
        ]
    return _finish(w, s, history)


def sanctioned_one_hop(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.HOLD_FOR_REVIEW,
            RiskLevel.MEDIUM,
            "sanctioned_one_hop",
            "The destination is not listed, but it recently received a large share of its "
            "funds directly from a sanctioned address; an analyst must review.",
        ),
        amount=round(w.rng.uniform(2, 50), 2),
    )
    s.profiles[addr] = w.profile(
        addr, age_days=w.rng.uniform(10, 200), tx_count=w.rng.randint(8, 80), balance_eth=12
    )
    listed = w.address()
    w.counterparty(s, listed, w.profile(listed, age_days=600, tx_count=300))
    s.sanctions[listed] = SanctionHit(
        address=listed,
        list_name="OFAC SDN",
        entity_name=f"SYNTHETIC SANCTIONED ENTITY {i + 41:02d}",
        program="CYBER2",
    )
    peers = _ordinary_peers(w, s, 2)
    history = [w.transfer(listed, addr, w.rng.uniform(20, 120), w.rng.uniform(1, 6))]
    history += [w.transfer(p, addr, w.rng.uniform(0.1, 2), w.rng.uniform(5, 60)) for p in peers]
    return _finish(w, s, history)


def fresh_large_inflow(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.HOLD_FOR_REVIEW,
            RiskLevel.MEDIUM,
            "fresh_large_inflow",
            "A days-old address with no history received large amounts from several fresh "
            "addresses and is the target of a large withdrawal from a young account.",
        ),
        amount=round(w.rng.uniform(50, 400), 1),
        account_context={"account_age_days": w.rng.randint(2, 20), "first_withdrawal": True},
    )
    s.profiles[addr] = w.profile(
        addr, age_days=w.rng.uniform(0.2, 2), tx_count=w.rng.randint(3, 9), balance_eth=150
    )
    history = []
    for _ in range(w.rng.randint(3, 6)):
        src = w.address()
        w.counterparty(s, src, w.profile(src, age_days=w.rng.uniform(0.5, 5), tx_count=4))
        history.append(w.transfer(src, addr, w.rng.uniform(20, 90), w.rng.uniform(0, 2)))
    return _finish(w, s, history)


def nonblocking_scam_report(w: World, i: int) -> Scenario:
    addr = w.address()
    category = ["ponzi", "honeypot", "suspicious_activity", "fake_token", "ponzi"][i % 5]
    s = Scenario(
        addr,
        _label(
            Action.HOLD_FOR_REVIEW,
            RiskLevel.MEDIUM,
            "nonblocking_scam_report",
            f"A public feed reports the destination for '{category}', a category that "
            "warrants review but not an automatic block.",
        ),
        amount=round(w.rng.uniform(0.5, 20), 2),
    )
    s.profiles[addr] = w.profile(
        addr,
        age_days=w.rng.uniform(30, 400),
        tx_count=w.rng.randint(100, 3000),
        contract=category in {"honeypot", "fake_token"},
    )
    peers = _ordinary_peers(w, s, 4)
    history = [w.transfer(p, addr, w.rng.uniform(0.1, 3), w.rng.uniform(0, 30)) for p in peers]
    s.scam_reports[addr] = [
        ScamReport(
            address=addr,
            category=category,
            source="synthetic-feed",
            description=f"Community report: {category.replace('_', ' ')}.",
        )
    ]
    return _finish(w, s, history)


def mixer_exposure(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.HOLD_FOR_REVIEW,
            RiskLevel.MEDIUM,
            "mixer_exposure",
            "Most of the destination's funds arrived directly from a mixing contract; "
            "exposure to mixers requires review.",
        ),
        amount=round(w.rng.uniform(1, 30), 2),
    )
    s.profiles[addr] = w.profile(
        addr, age_days=w.rng.uniform(3, 60), tx_count=w.rng.randint(4, 30), balance_eth=9
    )
    mixer = w.address()
    w.counterparty(
        s,
        mixer,
        w.profile(
            mixer,
            age_days=1500,
            tx_count=90000,
            contract=True,
            labels=["Mixer: privacy pool (100 ETH)"],
        ),
    )
    history = [w.transfer(mixer, addr, 100.0, w.rng.uniform(0.5, 10)) for _ in range(1 + i % 2)]
    peers = _ordinary_peers(w, s, 2)
    history += [w.transfer(addr, p, w.rng.uniform(5, 40), w.rng.uniform(0, 0.5)) for p in peers]
    return _finish(w, s, history)


def peel_chain(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.HOLD_FOR_REVIEW,
            RiskLevel.MEDIUM,
            "peel_chain",
            "A young address receives one large amount and forwards it in a chain of "
            "shrinking hops to fresh addresses, a layering pattern.",
        ),
        amount=round(w.rng.uniform(5, 80), 2),
    )
    s.profiles[addr] = w.profile(addr, age_days=w.rng.uniform(1, 8), tx_count=12, balance_eth=3)
    src = w.address()
    w.counterparty(s, src, w.profile(src, age_days=3, tx_count=6))
    start = w.rng.uniform(150, 400)
    history = [w.transfer(src, addr, start, 1.5)]
    remaining = start
    for hop in range(6):
        out = w.address()
        w.counterparty(s, out, w.profile(out, age_days=0.2, tx_count=1))
        piece = remaining * w.rng.uniform(0.08, 0.15)
        remaining -= piece
        history.append(w.transfer(addr, out, piece, 1.4 - hop * 0.2))
    return _finish(w, s, history)


def benign_eoa(w: World, i: int) -> Scenario:
    addr = w.address()
    previous = w.rng.randint(1, 12)
    s = Scenario(
        addr,
        _label(
            Action.ALLOW,
            RiskLevel.NONE,
            "benign_eoa",
            "A years-old, steadily active personal address with ordinary transfers to and "
            "from long-lived counterparties; the account has withdrawn to it before.",
        ),
        amount=round(w.rng.uniform(0.05, 8), 3),
        account_context={
            "account_age_days": w.rng.randint(400, 2500),
            "previous_withdrawals_to_address": previous,
        },
    )
    s.profiles[addr] = w.profile(
        addr,
        age_days=w.rng.uniform(700, 3000),
        tx_count=w.rng.randint(200, 5000),
        balance_eth=w.rng.uniform(0.5, 30),
        last_days=w.rng.uniform(0.1, 5),
    )
    peers = _ordinary_peers(w, s, 5)
    history = []
    for p in peers:
        history.append(w.transfer(p, addr, w.rng.uniform(0.01, 3), w.rng.uniform(0, 60)))
        history.append(w.transfer(addr, p, w.rng.uniform(0.01, 2), w.rng.uniform(0, 60)))
    return _finish(w, s, history)


def benign_contract(w: World, i: int) -> Scenario:
    addr = w.address()
    kind = ["DEX router", "Lending pool", "Staking deposit contract", "Token bridge", "DEX pair"][
        i % 5
    ]
    s = Scenario(
        addr,
        _label(
            Action.ALLOW,
            RiskLevel.NONE,
            "benign_contract",
            f"A long-lived, heavily used, labeled protocol contract ({kind.lower()}); "
            "deposits into it are routine.",
        ),
        amount=round(w.rng.uniform(0.1, 25), 3),
        account_context={"account_age_days": w.rng.randint(100, 2000)},
    )
    s.profiles[addr] = w.profile(
        addr,
        age_days=w.rng.uniform(900, 2800),
        tx_count=w.rng.randint(200_000, 5_000_000),
        contract=True,
        balance_eth=w.rng.uniform(100, 50_000),
        last_days=0.01,
        labels=[f"{kind} (verified contract)"],
    )
    peers = _ordinary_peers(w, s, 6)
    history = [w.transfer(p, addr, w.rng.uniform(0.01, 10), w.rng.uniform(0, 0.2)) for p in peers]
    return _finish(w, s, history)


def benign_fresh_small(w: World, i: int) -> Scenario:
    addr = w.address()
    s = Scenario(
        addr,
        _label(
            Action.ALLOW,
            RiskLevel.NONE,
            "benign_fresh_small",
            "A new address with no adverse signals receiving a small amount from an "
            "established account; freshness alone does not justify friction.",
        ),
        amount=round(w.rng.uniform(0.02, 0.3), 3),
        account_context={"account_age_days": w.rng.randint(500, 2000)},
    )
    s.profiles[addr] = w.profile(addr, age_days=w.rng.uniform(1, 6), tx_count=1, balance_eth=0.05)
    peers = _ordinary_peers(w, s, 1)
    return _finish(w, s, [w.transfer(peers[0], addr, 0.05, w.rng.uniform(1, 5))])


Builder = Callable[[World, int], Scenario]

SCENARIOS: dict[str, tuple[Builder, int]] = {
    "sanctioned_direct": (sanctioned_direct, 8),
    "drainer_report": (drainer_report, 8),
    "address_poisoning": (address_poisoning, 8),
    "sanctioned_one_hop": (sanctioned_one_hop, 6),
    "fresh_large_inflow": (fresh_large_inflow, 8),
    "nonblocking_scam_report": (nonblocking_scam_report, 5),
    "mixer_exposure": (mixer_exposure, 5),
    "peel_chain": (peel_chain, 4),
    "benign_eoa": (benign_eoa, 10),
    "benign_contract": (benign_contract, 5),
    "benign_fresh_small": (benign_fresh_small, 4),
}


def _interleaved(w: World) -> Iterator[tuple[str, Scenario]]:
    """Round-robin over scenario families so any prefix of the dataset is mixed."""
    built = {
        name: [builder(w, i) for i in range(count)] for name, (builder, count) in SCENARIOS.items()
    }
    for row in itertools.zip_longest(*built.values()):
        for name, scenario in zip(built, row, strict=True):
            if scenario is not None:
                yield name, scenario


def real_cases() -> list[tuple[str, float, Label]]:
    cases = [
        (
            address,
            1.0,
            Label(
                expected_action=Action.BLOCK,
                min_risk_level=RiskLevel.CRITICAL,
                scenario="ofac_listed",
                source=LabelSource.OFAC,
                rationale=f"Listed on the OFAC SDN snapshot (program {program}).",
            ),
        )
        for address, program in OFAC_ADDRESSES
    ]
    cases += [
        (
            address,
            0.5,
            Label(
                expected_action=Action.ALLOW,
                min_risk_level=RiskLevel.NONE,
                scenario="benign_protocol",
                source=LabelSource.BENIGN_PUBLIC,
                rationale=f"Widely used, long-lived {description}; no adverse reports.",
            ),
        )
        for address, description in PROTOCOL_CONTRACTS
    ]
    return cases


def build(seed: int = SEED) -> tuple[list[EvalCase], list[Snapshot]]:
    w = World(seed)
    cases: list[EvalCase] = []
    snapshots: list[Snapshot] = []
    smoke_left = set(SMOKE_SCENARIOS)
    for number, (name, s) in enumerate(_interleaved(w), start=1):
        case_id = f"ev-{number:04d}"
        tags = ["synthetic"]
        if name in smoke_left:
            smoke_left.discard(name)
            tags.append("smoke")
        cases.append(
            EvalCase(
                case_id=case_id,
                address=s.address,
                amount=s.amount,
                asset=s.asset,
                account_context=s.account_context,
                label=s.label,
                tags=tags,
            )
        )
        snapshots.append(
            Snapshot(
                case_id=case_id,
                origin=SnapshotOrigin.SYNTHETIC,
                recorded_at=REFERENCE_TIME,
                sources={p: "synthetic" for p in ("chain_data", "sanctions", "scam_intel")},
                profiles=s.profiles,
                transfers=s.transfers,
                sanctions=s.sanctions,
                scam_reports=s.scam_reports,
            )
        )
    for address, amount, label in real_cases():
        cases.append(
            EvalCase(
                case_id=f"ev-{len(cases) + 1:04d}",
                address=address,
                amount=amount,
                label=label,
                tags=["real"],
            )
        )
    return cases, snapshots


def write(
    cases_file: Path = CASES_FILE, snapshots_dir: Path = SNAPSHOTS_DIR, seed: int = SEED
) -> tuple[int, int]:
    cases, snapshots = build(seed)
    write_cases(cases, cases_file)
    for snapshot in snapshots:
        path = snapshots_dir / f"{snapshot.case_id}.json"
        if path.exists() and read_snapshot(path).origin is SnapshotOrigin.RECORDED:
            continue
        write_snapshot(snapshot, snapshots_dir)
    return len(cases), len(snapshots)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    n_cases, n_snapshots = write(seed=args.seed)
    print(json.dumps({"cases": n_cases, "synthetic_snapshots": n_snapshots}))


if __name__ == "__main__":
    main()
