import os
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from sentinel.core.ports import SanctionsSource
from sentinel.tools.sanctions import (
    DEFAULT_SNAPSHOT,
    SDN_XML_URL,
    OfacSanctions,
    SdnSnapshot,
    parse_sdn_xml,
    refresh_snapshot,
)

SAMPLE = Path(__file__).parent / "fixtures" / "sdn_sample.xml"
GROUP_ETH = "0x5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
GROUP_USDT = "0x6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b"
PERSON_ETH = "0x7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c7c"


def test_parse_keeps_only_evm_addresses() -> None:
    publish_date, addresses = parse_sdn_xml(SAMPLE)
    assert publish_date == date(2026, 10, 21)
    assert [a.address for a in addresses] == [GROUP_ETH, GROUP_USDT, PERSON_ETH]
    by_address = {a.address: a for a in addresses}
    assert by_address[GROUP_ETH].programs == ["CYBER2", "DPRK3"]
    assert by_address[GROUP_USDT].currency == "USDT"
    assert by_address[PERSON_ETH].entity_name == "Jane EXAMPLE"


async def test_check_hit_is_case_insensitive() -> None:
    source = OfacSanctions.from_sdn_xml(SAMPLE)
    assert isinstance(source, SanctionsSource)
    hit = await source.check(GROUP_ETH.upper().replace("0X", "0x"))
    assert hit is not None
    assert hit.address == GROUP_ETH
    assert hit.list_name == "OFAC SDN"
    assert hit.entity_name == "EXAMPLE CYBER GROUP"
    assert hit.program == "CYBER2, DPRK3"
    assert hit.listed_on == datetime(2026, 10, 21, tzinfo=UTC)


async def test_check_miss_and_bad_input() -> None:
    source = OfacSanctions.from_sdn_xml(SAMPLE)
    assert await source.check("0x" + "00" * 20) is None
    assert await source.check("not-an-address") is None


@respx.mock
async def test_refresh_snapshot_round_trip(tmp_path: Path) -> None:
    respx.get(SDN_XML_URL).mock(return_value=httpx.Response(200, content=SAMPLE.read_bytes()))
    out = tmp_path / "snapshot.json"
    snapshot = await refresh_snapshot(out)
    assert len(snapshot.addresses) == 3
    source = OfacSanctions.from_snapshot(out)
    assert len(source) == 3
    assert await source.check(PERSON_ETH) is not None


def test_committed_snapshot_is_valid() -> None:
    snapshot = SdnSnapshot.model_validate_json(DEFAULT_SNAPSHOT.read_text())
    assert len(snapshot.addresses) > 50
    assert all(a.address == a.address.lower() for a in snapshot.addresses)


@pytest.mark.live
async def test_live_ofac_download(tmp_path: Path) -> None:
    if not os.environ.get("SENTINEL_LIVE_OFAC"):
        pytest.skip("set SENTINEL_LIVE_OFAC=1 to download the ~30 MB SDN list")
    snapshot = await refresh_snapshot(tmp_path / "live.json")
    assert len(snapshot.addresses) > 50
