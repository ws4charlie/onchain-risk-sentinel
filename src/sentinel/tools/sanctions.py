"""`SanctionsSource` backed by the OFAC SDN list (U.S. Treasury, public domain).

The official SDN XML lists digital currency addresses as `<id>` entries with
`idType` "Digital Currency Address - <TICKER>". We keep every entry whose value is an
EVM address (ETH, and EVM-format USDT/USDC/ARB/BSC/... addresses), because the same
address controls funds on Ethereum mainnet.

A small JSON snapshot extracted from the XML is committed under `data/sanctions/`;
`refresh_snapshot` rebuilds it from the live list.
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict

from sentinel.core.schemas import SanctionHit, normalize_address
from sentinel.tools._http import request_with_retry
from sentinel.tools.paths import DATA_DIR

SDN_XML_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
SDN_SEARCH_URL = "https://sanctionssearch.ofac.treas.gov/"
LIST_NAME = "OFAC SDN"
DEFAULT_SNAPSHOT = DATA_DIR / "sanctions" / "ofac_sdn_evm_addresses.json"

_ID_TYPE_PREFIX = "Digital Currency Address - "


class SdnAddress(BaseModel):
    """One sanctioned address as stored in the snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str
    currency: str
    entity_name: str
    programs: list[str]
    sdn_uid: int


class SdnSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_url: str
    publish_date: date | None
    retrieved_at: datetime
    addresses: list[SdnAddress]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(elem: ET.Element, name: str) -> str:
    for child in elem:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _children(elem: ET.Element, name: str) -> Iterable[ET.Element]:
    return (child for child in elem.iter() if _local(child.tag) == name)


def _entity_name(entry: ET.Element) -> str:
    first = _child_text(entry, "firstName")
    last = _child_text(entry, "lastName")
    return f"{first} {last}".strip() if first else last


def parse_sdn_xml(source: str | Path | bytes) -> tuple[date | None, list[SdnAddress]]:
    """Extract the publish date and all EVM-format digital currency addresses."""
    root = ET.fromstring(source) if isinstance(source, bytes) else ET.parse(source).getroot()
    publish_date: date | None = None
    for info in _children(root, "publshInformation"):  # sic, OFAC's spelling
        raw = _child_text(info, "Publish_Date")
        if raw:
            publish_date = datetime.strptime(raw, "%m/%d/%Y").date()

    found: dict[str, SdnAddress] = {}
    for entry in _children(root, "sdnEntry"):
        name = _entity_name(entry)
        programs = [p.text.strip() for p in _children(entry, "program") if p.text]
        uid = int(_child_text(entry, "uid") or 0)
        for id_elem in _children(entry, "id"):
            id_type = _child_text(id_elem, "idType")
            if not id_type.startswith(_ID_TYPE_PREFIX):
                continue
            try:
                address = normalize_address(_child_text(id_elem, "idNumber"))
            except ValueError:
                continue  # non-EVM address (BTC, TRX, ...)
            found.setdefault(
                address,
                SdnAddress(
                    address=address,
                    currency=id_type.removeprefix(_ID_TYPE_PREFIX),
                    entity_name=name,
                    programs=programs,
                    sdn_uid=uid,
                ),
            )
    return publish_date, sorted(found.values(), key=lambda a: a.address)


async def download_sdn_xml(
    client: httpx.AsyncClient | None = None, url: str = SDN_XML_URL, request_timeout: float = 120.0
) -> bytes:
    """Download the official SDN XML (about 30 MB; the URL redirects to a signed S3 link)."""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=request_timeout, follow_redirects=True)
    try:
        response = await request_with_retry(
            lambda: client.get(url, follow_redirects=True), max_retries=3, backoff_base=2.0
        )
        return response.content
    finally:
        if owns:
            await client.aclose()


async def refresh_snapshot(
    path: Path = DEFAULT_SNAPSHOT, client: httpx.AsyncClient | None = None
) -> SdnSnapshot:
    """Download the live SDN list, extract EVM addresses and write the snapshot JSON."""
    raw = await download_sdn_xml(client)
    publish_date, addresses = await asyncio.to_thread(parse_sdn_xml, raw)
    snapshot = SdnSnapshot(
        source_url=SDN_XML_URL,
        publish_date=publish_date,
        retrieved_at=datetime.now(UTC).replace(microsecond=0),
        addresses=addresses,
    )
    await asyncio.to_thread(_write_snapshot, path, snapshot)
    return snapshot


def _write_snapshot(path: Path, snapshot: SdnSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")


class OfacSanctions:
    """In-memory lookup over OFAC SDN addresses. Lookups are case-insensitive."""

    def __init__(self, addresses: Iterable[SdnAddress], publish_date: date | None = None) -> None:
        self._by_address = {a.address: a for a in addresses}
        self._listed_on = (
            datetime(publish_date.year, publish_date.month, publish_date.day, tzinfo=UTC)
            if publish_date
            else None
        )

    @classmethod
    def from_snapshot(cls, path: Path = DEFAULT_SNAPSHOT) -> Self:
        snapshot = SdnSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        return cls(snapshot.addresses, snapshot.publish_date)

    @classmethod
    def from_sdn_xml(cls, source: str | Path | bytes) -> Self:
        publish_date, addresses = parse_sdn_xml(source)
        return cls(addresses, publish_date)

    def __len__(self) -> int:
        return len(self._by_address)

    async def check(self, address: str) -> SanctionHit | None:
        try:
            key = normalize_address(address)
        except ValueError:
            return None
        entry = self._by_address.get(key)
        if entry is None:
            return None
        return SanctionHit(
            address=key,
            list_name=LIST_NAME,
            entity_name=entry.entity_name,
            program=", ".join(entry.programs) or None,
            # The SDN XML has no per-address listing date; this is the list's publish date.
            listed_on=self._listed_on,
            source_url=SDN_SEARCH_URL,
        )
