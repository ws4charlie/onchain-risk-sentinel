"""`ScamIntelSource` backed by open scam/phishing address datasets.

Feeds are downloaded at runtime into a git-ignored cache directory and never
committed: ScamSniffer's database is GPL-3.0, and the Forta datasets are derived from
third-party labels. See `data/SOURCES.md` for URLs, licenses and freshness notes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Self

import httpx

from sentinel.core.schemas import ScamReport, normalize_address
from sentinel.tools._http import request_with_retry
from sentinel.tools.paths import CACHE_DIR

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = CACHE_DIR / "scam_intel"


@dataclass(frozen=True)
class ScamFeed:
    """One downloadable dataset and how to turn it into reports."""

    name: str
    url: str
    filename: str
    parse: Callable[[bytes], Iterable[ScamReport]]


def _safe_address(value: str) -> str | None:
    try:
        return normalize_address(value)
    except ValueError:
        return None


def _csv_rows(raw: bytes) -> Iterator[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(raw.decode("utf-8")))


_FORTA_URL = "https://github.com/forta-network/labelled-datasets"
_SCAMSNIFFER_URL = "https://github.com/scamsniffer/scam-database"


def parse_scamsniffer_addresses(raw: bytes) -> Iterator[ScamReport]:
    """`blacklist/address.json`: a JSON array of addresses."""
    for value in json.loads(raw):
        address = _safe_address(str(value))
        if address:
            yield ScamReport(
                address=address,
                category="phishing",
                source="scamsniffer",
                description="Listed in the ScamSniffer scam-database address blacklist "
                "(wallet drainer / phishing operations).",
                source_url=_SCAMSNIFFER_URL,
            )


def parse_forta_phishing(raw: bytes) -> Iterator[ScamReport]:
    """`labels/1/phishing_scams.csv`: address, etherscan_tag, etherscan_labels, is_contract."""
    for row in _csv_rows(raw):
        address = _safe_address(row.get("address", ""))
        if address:
            tag = row.get("etherscan_tag", "").strip()
            yield ScamReport(
                address=address,
                category="phishing",
                source="forta-labelled-datasets",
                description=f"Phishing scam address (public label: {tag or 'unnamed'}).",
                source_url=_FORTA_URL,
            )


def _contract_category(label: str) -> str:
    label = label.lower()
    if "phish" in label:
        return "phishing"
    if "heist" in label:
        return "heist"
    if "exploit" in label:
        return "exploit"
    return "malicious_contract"


def parse_forta_malicious_contracts(raw: bytes) -> Iterator[ScamReport]:
    """`labels/1/malicious_smart_contracts.csv`: contracts and their deployers."""
    for row in _csv_rows(raw):
        label = row.get("contract_creator_etherscan_label", "")
        category = _contract_category(label)
        tag = row.get("contract_tag", "").strip() or "unnamed"
        contract = _safe_address(row.get("contract_address", ""))
        if contract:
            yield ScamReport(
                address=contract,
                category=category,
                source="forta-labelled-datasets",
                description=f"Malicious smart contract ({category}; public tag: {tag}).",
                source_url=_FORTA_URL,
            )
        creator = _safe_address(row.get("contract_creator", ""))
        if creator:
            yield ScamReport(
                address=creator,
                category=f"{category}_deployer",
                source="forta-labelled-datasets",
                description=f"Deployed a malicious smart contract ({category}; tag: {tag}).",
                source_url=_FORTA_URL,
            )


def parse_forta_etherscan_labels(raw: bytes) -> Iterator[ScamReport]:
    """`labels/1/etherscan_malicious_labels.csv`: banned_address, wallet_tag, data_source."""
    for row in _csv_rows(raw):
        address = _safe_address(row.get("banned_address", ""))
        if address:
            data_source = row.get("data_source", "")
            yield ScamReport(
                address=address,
                category="phishing" if "phish" in data_source else "malicious",
                source="forta-labelled-datasets",
                description=f"Flagged by a public block-explorer label "
                f"({row.get('wallet_tag', '').strip() or 'unnamed'}).",
                source_url=_FORTA_URL,
            )


_FORTA_RAW = "https://raw.githubusercontent.com/forta-network/labelled-datasets/main/labels/1"

DEFAULT_FEEDS: tuple[ScamFeed, ...] = (
    ScamFeed(
        name="scamsniffer-addresses",
        url="https://raw.githubusercontent.com/scamsniffer/scam-database/main/blacklist/address.json",
        filename="scamsniffer_address.json",
        parse=parse_scamsniffer_addresses,
    ),
    ScamFeed(
        name="forta-phishing-scams",
        url=f"{_FORTA_RAW}/phishing_scams.csv",
        filename="forta_phishing_scams.csv",
        parse=parse_forta_phishing,
    ),
    ScamFeed(
        name="forta-malicious-contracts",
        url=f"{_FORTA_RAW}/malicious_smart_contracts.csv",
        filename="forta_malicious_smart_contracts.csv",
        parse=parse_forta_malicious_contracts,
    ),
    ScamFeed(
        name="forta-etherscan-labels",
        url=f"{_FORTA_RAW}/etherscan_malicious_labels.csv",
        filename="forta_etherscan_malicious_labels.csv",
        parse=parse_forta_etherscan_labels,
    ),
)


def _is_fresh(path: Path, max_age: timedelta) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < max_age.total_seconds()


def _write_atomic(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


async def sync_feeds(
    feeds: Iterable[ScamFeed] = DEFAULT_FEEDS,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    max_age: timedelta = timedelta(days=1),
    client: httpx.AsyncClient | None = None,
) -> None:
    """Download feeds whose cached copy is missing or older than `max_age`.

    A failed download is logged and the stale copy (if any) is kept, so one dead
    feed does not stop triage.
    """
    await asyncio.to_thread(cache_dir.mkdir, parents=True, exist_ok=True)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
    try:
        for feed in feeds:
            path = cache_dir / feed.filename
            if await asyncio.to_thread(_is_fresh, path, max_age):
                continue

            async def send(url: str = feed.url) -> httpx.Response:
                return await client.get(url)

            try:
                response = await request_with_retry(send, max_retries=3, backoff_base=1.0)
            except httpx.HTTPError as exc:
                logger.warning("scam feed %s download failed: %r", feed.name, exc)
                continue
            await asyncio.to_thread(_write_atomic, path, response.content)
    finally:
        if owns:
            await client.aclose()


class FeedScamIntel:
    """In-memory index of scam reports from one or more feeds."""

    def __init__(self, reports: Iterable[ScamReport]) -> None:
        index: dict[str, list[ScamReport]] = defaultdict(list)
        for report in reports:
            bucket = index[report.address]
            if report not in bucket:
                bucket.append(report)
        self._index = dict(index)

    @classmethod
    def from_cache(
        cls, cache_dir: Path = DEFAULT_CACHE_DIR, feeds: Iterable[ScamFeed] = DEFAULT_FEEDS
    ) -> Self:
        """Build from cached feed files; missing or unparsable feeds are skipped."""
        reports: list[ScamReport] = []
        for feed in feeds:
            path = cache_dir / feed.filename
            if not path.exists():
                logger.warning("scam feed %s not cached at %s", feed.name, path)
                continue
            try:
                reports.extend(feed.parse(path.read_bytes()))
            except (ValueError, KeyError, UnicodeDecodeError) as exc:
                logger.warning("scam feed %s could not be parsed: %r", feed.name, exc)
        return cls(reports)

    @classmethod
    async def load(
        cls,
        cache_dir: Path = DEFAULT_CACHE_DIR,
        feeds: Iterable[ScamFeed] = DEFAULT_FEEDS,
        *,
        max_age: timedelta = timedelta(days=1),
        client: httpx.AsyncClient | None = None,
    ) -> Self:
        """Refresh stale feeds, then build from the cache."""
        feeds = tuple(feeds)
        await sync_feeds(feeds, cache_dir, max_age=max_age, client=client)
        return await asyncio.to_thread(cls.from_cache, cache_dir, feeds)

    def __len__(self) -> int:
        return len(self._index)

    async def lookup(self, address: str) -> list[ScamReport]:
        try:
            key = normalize_address(address)
        except ValueError:
            return []
        return list(self._index.get(key, []))
