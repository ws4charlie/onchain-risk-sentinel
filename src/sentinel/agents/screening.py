"""Deterministic counterparty screening and flow features over the ports.

These checks look past the destination itself: who funded it, where its money
went, and how. They are plain functions so the specialist nodes can run them
as pre-checks (no model choice involved) and the agents can also call them as
tools. Every threshold is a module constant; the reasoning is next to it.

Amounts are only comparable within one asset (no price conversion here), so
flow numbers use the destination's *primary asset*: the asset with the most
non-zero transfers in the recent history, ties broken in favor of ETH.
Zero-value transfers are ignored: they move nothing and are a known spam and
address-poisoning vector.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sentinel.core.ports import Ports
from sentinel.core.schemas import AddressProfile, Chain, Evidence, Transfer

# How many counterparties (by transferred value) are screened against sanctions and
# scam intel. The largest flows carry the exposure; a bound keeps cost per case fixed.
TOP_COUNTERPARTIES = 5
# How many counterparties are profiled for flow features. Higher than the screening
# bound because peel-chain hops are small by construction and rank low by value.
MAX_PROFILE_LOOKUPS = 20
# Recent transfers read per case.
TRANSFER_WINDOW = 50

# An address younger than a week at request time has too little history to vouch for
# it; one-time mule and layering addresses typically live for hours to days.
FRESH_DAYS = 7.0

# Substrings (case-insensitive) of address labels or contract names that identify a
# mixing service, e.g. explorer contract names such as "TornadoCash_Eth" or
# "TornadoRouter", or labels like "Mixer: ...". Kept short and explicit on purpose.
MIXER_MARKERS: tuple[str, ...] = ("tornado", "mixer", "mixing", "railgun", "privacy pool")


@dataclass(frozen=True)
class Counterparty:
    address: str
    inflow: float
    outflow: float
    inflow_share: float
    outflow_share: float

    @property
    def direction(self) -> str:
        if self.inflow and self.outflow:
            return "both"
        return "in" if self.inflow else "out"

    @property
    def exposure_share(self) -> float:
        """The larger of its shares of the destination's inflow and outflow."""
        return max(self.inflow_share, self.outflow_share)

    def as_data(self, asset: str) -> dict[str, object]:
        return {
            "counterparty": self.address,
            "direction": self.direction,
            "asset": asset,
            "inflow": round(self.inflow, 8),
            "outflow": round(self.outflow, 8),
            "inflow_share": round(self.inflow_share, 4),
            "outflow_share": round(self.outflow_share, 4),
            "exposure_share": round(self.exposure_share, 4),
        }


def primary_asset(transfers: Sequence[Transfer]) -> str | None:
    counts = Counter(t.asset for t in transfers if t.amount > 0)
    if not counts:
        return None
    return max(counts, key=lambda a: (counts[a], a == "ETH"))


def _valued(transfers: Sequence[Transfer], asset: str) -> list[Transfer]:
    return [t for t in transfers if t.asset == asset and t.amount > 0]


def counterparties(address: str, transfers: Sequence[Transfer]) -> tuple[str, list[Counterparty]]:
    """Counterparties in the primary asset, largest total flow first."""
    asset = primary_asset(transfers)
    if asset is None:
        return "", []
    inflow: defaultdict[str, float] = defaultdict(float)
    outflow: defaultdict[str, float] = defaultdict(float)
    for t in _valued(transfers, asset):
        if t.to_address == address and t.from_address != address:
            inflow[t.from_address] += t.amount
        elif t.from_address == address and t.to_address != address:
            outflow[t.to_address] += t.amount
    total_in, total_out = sum(inflow.values()), sum(outflow.values())
    parties = [
        Counterparty(
            address=a,
            inflow=inflow[a],
            outflow=outflow[a],
            inflow_share=inflow[a] / total_in if total_in else 0.0,
            outflow_share=outflow[a] / total_out if total_out else 0.0,
        )
        for a in set(inflow) | set(outflow)
    ]
    parties.sort(key=lambda c: (-(c.inflow + c.outflow), c.address))
    return asset, parties


def age_days(profile: AddressProfile, at: datetime) -> float | None:
    if profile.first_seen is None:
        return None
    return max((at - profile.first_seen).total_seconds() / 86400, 0.0)


def is_fresh(profile: AddressProfile, at: datetime) -> bool:
    """Younger than FRESH_DAYS, or no history at all (unknown first_seen and nonce 0)."""
    age = age_days(profile, at)
    if age is None:
        return profile.tx_count == 0
    return age < FRESH_DAYS


def matches_mixer(profile: AddressProfile) -> str | None:
    """Return the first label that names a mixer, if any."""
    for label in profile.labels:
        lowered = label.lower()
        if any(marker in lowered for marker in MIXER_MARKERS):
            return label
    return None


async def _top(ports: Ports, address: str) -> tuple[str, list[Counterparty]]:
    transfers = await ports.chain_data.get_recent_transfers(
        Chain.ETHEREUM, address, TRANSFER_WINDOW
    )
    asset, parties = counterparties(address, transfers)
    return asset, parties[:TOP_COUNTERPARTIES]


def _clear(kind_source: str, address: str, screened: list[str], failed: list[str]) -> Evidence:
    summary = f"None of the top {len(screened)} counterparties of {address} {kind_source}"
    if failed:
        summary += f" ({len(failed)} could not be checked)"
    return Evidence(
        source="counterparty_screening",
        kind="counterparty_clear",
        summary=summary,
        data={"address": address, "screened": screened, "unchecked": failed},
    )


async def screen_counterparty_sanctions(ports: Ports, address: str) -> list[Evidence]:
    """Screen the destination's largest counterparties against the sanctions list."""
    asset, parties = await _top(ports, address)
    evidence: list[Evidence] = []
    failed: list[str] = []
    for party in parties:
        try:
            hit = await ports.sanctions.check(party.address)
        except Exception:
            failed.append(party.address)
            continue
        if hit is not None:
            evidence.append(
                Evidence(
                    source=hit.list_name,
                    kind="counterparty_sanctions_hit",
                    summary=f"Counterparty {party.address} ({party.direction}, "
                    f"{party.exposure_share:.0%} of {asset} flow) is listed on "
                    f"{hit.list_name} as {hit.entity_name}",
                    data={
                        **party.as_data(asset),
                        "list_name": hit.list_name,
                        "entity_name": hit.entity_name,
                        "program": hit.program,
                    },
                    url=hit.source_url,
                )
            )
    if not evidence:
        evidence.append(
            _clear("is on a sanctions list", address, [p.address for p in parties], failed)
        )
    return evidence


async def screen_counterparty_intel(ports: Ports, address: str) -> list[Evidence]:
    """Look up scam reports and mixer labels for the destination's largest counterparties."""
    asset, parties = await _top(ports, address)
    evidence: list[Evidence] = []
    failed: list[str] = []
    for party in parties:
        try:
            reports = await ports.scam_intel.lookup(party.address)
            profile = await ports.chain_data.get_address_profile(Chain.ETHEREUM, party.address)
        except Exception:
            failed.append(party.address)
            continue
        for report in reports:
            evidence.append(
                Evidence(
                    source=report.source,
                    kind="counterparty_scam_report",
                    summary=f"Counterparty {party.address} ({party.direction}, "
                    f"{party.exposure_share:.0%} of {asset} flow) is reported as "
                    f"{report.category}",
                    data={**party.as_data(asset), "category": report.category},
                    url=report.source_url,
                )
            )
        mixer = matches_mixer(profile)
        if mixer is not None:
            evidence.append(
                Evidence(
                    source="chain_data",
                    kind="counterparty_mixer",
                    summary=f"Counterparty {party.address} ({party.direction}, "
                    f"{party.exposure_share:.0%} of {asset} flow) is a mixer: {mixer!r}",
                    data={**party.as_data(asset), "label": mixer},
                )
            )
    if not evidence:
        evidence.append(
            _clear(
                "has scam reports or a mixer label", address, [p.address for p in parties], failed
            )
        )
    return evidence


# Peel chain: one dominant inflow that is then forwarded in many small, shrinking slices
# to fresh addresses, so no single outflow matches the inflow amount.
# - the inflow must be at least half of all inflow (the chain moves one large sum);
# - at least 4 hops: paying two or three new addresses is ordinary, a sustained series
#   of slices is not;
# - each hop at most 30% of what is left, so the remainder shrinks step by step:
#   peeling takes small slices, unlike splitting a sum into equal halves or thirds.
#   Slice sizes themselves may vary; real chains do not peel exactly decreasing amounts.
PEEL_DOMINANT_INFLOW = 0.5
PEEL_MIN_HOPS = 4
PEEL_MAX_SLICE = 0.30


def peel_hops(address: str, transfers: Sequence[Transfer], asset: str, fresh: set[str]) -> int:
    """Length of the peel run after the dominant inflow (0 when there is none)."""
    valued = sorted(_valued(transfers, asset), key=lambda t: (t.timestamp, t.block_number))
    inflows = [t for t in valued if t.to_address == address and t.from_address != address]
    total_in = sum(t.amount for t in inflows)
    if not inflows or total_in == 0:
        return 0
    start = max(inflows, key=lambda t: t.amount)
    if start.amount < PEEL_DOMINANT_INFLOW * total_in:
        return 0
    remaining, hops = start.amount, 0
    for t in valued:
        if t.timestamp <= start.timestamp or t.from_address != address:
            continue
        if t.to_address not in fresh or t.amount > PEEL_MAX_SLICE * remaining:
            break
        hops += 1
        remaining -= t.amount
    return hops


async def compute_flow_features(ports: Ports, address: str, at: datetime) -> Evidence:
    """Summarize the destination's flows as one `flow_features` evidence item."""
    profile = await ports.chain_data.get_address_profile(Chain.ETHEREUM, address)
    transfers = await ports.chain_data.get_recent_transfers(
        Chain.ETHEREUM, address, TRANSFER_WINDOW
    )
    asset, parties = counterparties(address, transfers)
    fresh: set[str] = set()
    unchecked = 0
    for party in parties[:MAX_PROFILE_LOOKUPS]:
        try:
            other = await ports.chain_data.get_address_profile(Chain.ETHEREUM, party.address)
        except Exception:
            unchecked += 1
            continue
        if is_fresh(other, at):
            fresh.add(party.address)
    unchecked += max(len(parties) - MAX_PROFILE_LOOKUPS, 0)

    inflow = sum(p.inflow for p in parties)
    outflow = sum(p.outflow for p in parties)
    fresh_funders = [p for p in parties if p.inflow and p.address in fresh]
    fresh_inflow = sum(p.inflow for p in fresh_funders)
    hops = peel_hops(address, transfers, asset, fresh) if asset else 0
    age = age_days(profile, at)
    data: dict[str, object] = {
        "address": address,
        "asset": asset or None,
        "age_days": round(age, 2) if age is not None else None,
        "destination_fresh": is_fresh(profile, at),
        "transfers_considered": len(_valued(transfers, asset)) if asset else 0,
        "inflow_total": round(inflow, 8),
        "outflow_total": round(outflow, 8),
        "funder_count": sum(1 for p in parties if p.inflow),
        "fresh_funder_count": len(fresh_funders),
        "fresh_inflow_share": round(fresh_inflow / inflow, 4) if inflow else 0.0,
        "peel_hops": hops,
        "peel_chain": hops >= PEEL_MIN_HOPS,
        "unprofiled_counterparties": unchecked,
        "fresh_days": FRESH_DAYS,
    }
    age_text = f"{age:.1f} days old" if age is not None else "age unknown"
    summary = (
        f"{address} is {age_text} at request time; {data['funder_count']} funders sent "
        f"{inflow:g} {asset or 'units'}, {data['fresh_inflow_share']:.0%} of it from "
        f"{len(fresh_funders)} addresses younger than {FRESH_DAYS:g} days; outflow "
        f"{outflow:g}; peel-chain hops: {hops}"
    )
    return Evidence(source="chain_data", kind="flow_features", summary=summary, data=data)
