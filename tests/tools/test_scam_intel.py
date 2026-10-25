import os
import shutil
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import respx

from sentinel.core.ports import ScamIntelSource
from sentinel.tools.scam_intel import DEFAULT_FEEDS, FeedScamIntel, sync_feeds

FEEDS_DIR = Path(__file__).parent / "fixtures" / "scam_feeds"
DRAINER = "0xd4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1d4a1"


async def test_lookup_merges_feeds() -> None:
    source = FeedScamIntel.from_cache(FEEDS_DIR)
    assert isinstance(source, ScamIntelSource)
    reports = await source.lookup(DRAINER.upper().replace("0X", "0x"))
    assert {r.source for r in reports} == {"scamsniffer", "forta-labelled-datasets"}
    assert all(r.category == "phishing" and r.address == DRAINER for r in reports)


async def test_contract_and_deployer_categories() -> None:
    source = FeedScamIntel.from_cache(FEEDS_DIR)
    [contract] = await source.lookup("0x3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e3e")
    [deployer] = await source.lookup("0x4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d")
    assert contract.category == "exploit"
    assert deployer.category == "exploit_deployer"
    [labelled] = await source.lookup("0x2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b")
    assert labelled.category == "malicious"


async def test_unknown_and_bad_addresses_return_empty() -> None:
    source = FeedScamIntel.from_cache(FEEDS_DIR)
    assert await source.lookup("0x" + "00" * 20) == []
    assert await source.lookup("garbage") == []


def test_missing_feeds_are_skipped(tmp_path: Path) -> None:
    assert len(FeedScamIntel.from_cache(tmp_path)) == 0


@respx.mock
async def test_load_downloads_then_uses_cache(tmp_path: Path) -> None:
    routes = [
        respx.get(feed.url).mock(
            return_value=httpx.Response(200, content=(FEEDS_DIR / feed.filename).read_bytes())
        )
        for feed in DEFAULT_FEEDS
    ]
    source = await FeedScamIntel.load(tmp_path)
    assert await source.lookup(DRAINER)
    await FeedScamIntel.load(tmp_path)  # cache is fresh: no new downloads
    assert all(route.call_count == 1 for route in routes)


@respx.mock
async def test_failed_download_keeps_stale_copy(tmp_path: Path) -> None:
    for feed in DEFAULT_FEEDS:
        shutil.copy(FEEDS_DIR / feed.filename, tmp_path / feed.filename)
        respx.get(feed.url).mock(return_value=httpx.Response(404))
    await sync_feeds(DEFAULT_FEEDS, tmp_path, max_age=timedelta(0))
    source = FeedScamIntel.from_cache(tmp_path)
    assert await source.lookup(DRAINER)


@pytest.mark.live
async def test_live_feeds(tmp_path: Path) -> None:
    if not os.environ.get("SENTINEL_LIVE_FEEDS"):
        pytest.skip("set SENTINEL_LIVE_FEEDS=1 to download public scam feeds")
    source = await FeedScamIntel.load(tmp_path)
    assert len(source) > 1000
