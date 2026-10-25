"""Record port snapshots for real-address eval cases with the real adapters.

    uv run python -m sentinel.evals.record                 # every unrecorded real case
    uv run python -m sentinel.evals.record --ids ev-0072   # specific cases (re-records)

Per case it records the destination's profile and recent transfers, the
sanctions and scam-intel answers for the destination and its counterparties,
and the profiles of the busiest counterparties. It writes only
`evals/dataset/snapshots/<case_id>.json`; labels in `cases.jsonl` are never
touched, and synthetic snapshots are never replaced.

Needs SENTINEL_ETHERSCAN_API_KEY (chain data). Sanctions use the committed OFAC
snapshot; scam feeds are downloaded into the git-ignored cache. Scam reports are
stored as derived facts (category, feed name, our own description), never as
raw third-party rows; see `evals/README.md`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sentinel.core.config import Settings
from sentinel.core.ports import ChainDataSource, SanctionsSource, ScamIntelSource
from sentinel.core.schemas import Chain, ScamReport
from sentinel.evals.dataset import (
    CASES_FILE,
    SNAPSHOTS_DIR,
    EvalCase,
    LabelSource,
    Snapshot,
    SnapshotOrigin,
    read_cases,
    snapshot_path,
    write_snapshot,
)
from sentinel.tools import EtherscanChainData, FeedScamIntel, OfacSanctions

logger = logging.getLogger(__name__)


def _derived(report: ScamReport) -> ScamReport:
    """Keep only derived facts: drop free text copied from the feed."""
    return report.model_copy(
        update={
            "description": f"Listed by the {report.source} feed as {report.category}.",
            "reported_at": None,
        }
    )


async def record_case(
    case: EvalCase,
    chain: ChainDataSource,
    sanctions: SanctionsSource,
    scam_intel: ScamIntelSource,
    *,
    sources: dict[str, str],
    transfer_limit: int = 50,
    counterparty_profiles: int = 5,
) -> Snapshot:
    address = case.address
    profile = await chain.get_address_profile(Chain.ETHEREUM, address)
    transfers = await chain.get_recent_transfers(Chain.ETHEREUM, address, transfer_limit)
    peers = Counter(
        other for t in transfers for other in (t.from_address, t.to_address) if other != address
    )
    addresses = [address, *peers]
    profiles = {address: profile}
    for peer, _ in peers.most_common(counterparty_profiles):
        try:
            profiles[peer] = await chain.get_address_profile(Chain.ETHEREUM, peer)
        except Exception as exc:  # a missing counterparty profile is recorded as a miss
            logger.warning("profile for counterparty %s failed: %r", peer, exc)
    return Snapshot(
        case_id=case.case_id,
        origin=SnapshotOrigin.RECORDED,
        recorded_at=datetime.now(UTC),
        sources=sources,
        profiles=profiles,
        transfers={address: transfers},
        sanctions={a: await sanctions.check(a) for a in addresses},
        scam_reports={a: [_derived(r) for r in await scam_intel.lookup(a)] for a in addresses},
    )


def select_cases(
    cases: Sequence[EvalCase], ids: Sequence[str], snapshots_dir: Path
) -> list[EvalCase]:
    if ids:
        wanted = set(ids)
        chosen = [c for c in cases if c.case_id in wanted]
        synthetic = [c.case_id for c in chosen if c.label.source is LabelSource.SYNTHETIC]
        if synthetic:
            raise SystemExit(f"synthetic cases are generated, not recorded: {synthetic}")
        return chosen
    return [
        c
        for c in cases
        if c.label.source is not LabelSource.SYNTHETIC
        and not snapshot_path(c.case_id, snapshots_dir).exists()
    ]


async def run(
    ids: Sequence[str],
    *,
    cases_file: Path = CASES_FILE,
    snapshots_dir: Path = SNAPSHOTS_DIR,
    counterparty_profiles: int = 5,
) -> list[Path]:
    cases = select_cases(read_cases(cases_file), ids, snapshots_dir)
    if not cases:
        print("nothing to record")
        return []
    settings = Settings()
    sanctions = OfacSanctions.from_snapshot()
    scam_intel = await FeedScamIntel.load()
    sources = {"chain_data": "etherscan", "sanctions": "ofac_snapshot", "scam_intel": "feeds"}
    written: list[Path] = []
    async with EtherscanChainData.from_settings(settings) as chain:
        for case in cases:
            snapshot = await record_case(
                case,
                chain,
                sanctions,
                scam_intel,
                sources=sources,
                counterparty_profiles=counterparty_profiles,
            )
            written.append(write_snapshot(snapshot, snapshots_dir))
            print(f"recorded {case.case_id} ({case.address})")
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Record port snapshots for real eval cases.")
    parser.add_argument("--ids", nargs="*", default=[], help="Case ids (default: unrecorded).")
    parser.add_argument("--counterparty-profiles", type=int, default=5)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    paths = asyncio.run(run(args.ids, counterparty_profiles=args.counterparty_profiles))
    print(json.dumps({"written": [str(p) for p in paths]}))


if __name__ == "__main__":
    main()
