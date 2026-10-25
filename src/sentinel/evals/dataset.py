"""Eval dataset: labeled cases (JSONL) plus one recorded port snapshot per case (JSON).

Layout (see `evals/README.md`):

    evals/dataset/cases.jsonl              one `EvalCase` per line: input + label
    evals/dataset/snapshots/<case_id>.json  one `Snapshot`: every port response for the case

Labels and snapshots live in separate files so that re-recording data never
touches a label. A case without a snapshot is "unrecorded" and is not run.
Case ids are opaque (`ev-0001`) because the graph shows them to the models.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sentinel.core.schemas import (
    Action,
    AddressProfile,
    Case,
    RiskLevel,
    SanctionHit,
    ScamReport,
    Transfer,
    normalize_address,
)
from sentinel.tools.paths import REPO_ROOT

EVALS_DIR = REPO_ROOT / "evals"
DATASET_DIR = EVALS_DIR / "dataset"
CASES_FILE = DATASET_DIR / "cases.jsonl"
SNAPSHOTS_DIR = DATASET_DIR / "snapshots"

# Fixed so that runs are reproducible; synthetic snapshots are dated relative to it.
REFERENCE_TIME = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

RISK_ORDER: dict[RiskLevel, int] = {level: i for i, level in enumerate(RiskLevel)}


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LabelSource(StrEnum):
    OFAC = "ofac"  # address on the committed OFAC SDN snapshot
    SCAM_FEED = "scam_feed"  # address in a public scam feed
    BENIGN_PUBLIC = "benign_public"  # well-known protocol contract or long-lived active EOA
    SYNTHETIC = "synthetic"  # hand-built scenario with constructed port data


class Label(_Model):
    expected_action: Action
    min_risk_level: RiskLevel
    scenario: str = Field(description="Scenario family, e.g. 'drainer_report'.")
    source: LabelSource
    rationale: str = Field(min_length=10)

    @property
    def must_block(self) -> bool:
        return self.expected_action is Action.BLOCK

    @property
    def benign(self) -> bool:
        return self.expected_action is Action.ALLOW


class EvalCase(_Model):
    case_id: str = Field(pattern=r"^ev-\d{4}$")
    address: str
    amount: float | None = Field(default=None, ge=0)
    asset: str = "ETH"
    tx_hash: str | None = None
    account_context: dict[str, Any] = Field(default_factory=dict)
    label: Label
    tags: list[str] = Field(default_factory=list, description="e.g. 'smoke' for the CI subset.")

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return normalize_address(value)

    def to_case(self) -> Case:
        return Case(
            case_id=self.case_id,
            address=self.address,
            amount=self.amount,
            asset=self.asset,
            tx_hash=self.tx_hash,
            account_context=self.account_context,
            requested_at=REFERENCE_TIME,
        )


class SnapshotOrigin(StrEnum):
    SYNTHETIC = "synthetic"
    RECORDED = "recorded"


class Snapshot(_Model):
    """Every port response the agents may need for one case, keyed by address.

    An address missing from a map was not recorded; `RecordedPorts` reports
    that as a data-source error (the agents see an error, not "clean").
    `sanctions` maps an address to its hit, or None when it was checked and clean.
    """

    case_id: str
    origin: SnapshotOrigin
    recorded_at: datetime
    sources: dict[str, str] = Field(
        default_factory=dict, description="Port name -> adapter or 'synthetic'."
    )
    profiles: dict[str, AddressProfile] = Field(default_factory=dict)
    transfers: dict[str, list[Transfer]] = Field(default_factory=dict)
    sanctions: dict[str, SanctionHit | None] = Field(default_factory=dict)
    scam_reports: dict[str, list[ScamReport]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalized_keys(self) -> Snapshot:
        for name in ("profiles", "transfers", "sanctions", "scam_reports"):
            for key in getattr(self, name):
                if key != normalize_address(key):
                    raise ValueError(f"{name} key {key!r} is not a normalized address")
        return self

    def covers(self, address: str) -> bool:
        """True when every port has a recorded answer for `address`."""
        return all(
            address in m for m in (self.profiles, self.transfers, self.sanctions, self.scam_reports)
        )


class DatasetError(ValueError):
    pass


class Dataset(_Model):
    cases: list[EvalCase]
    snapshots: dict[str, Snapshot]

    def runnable(self) -> list[EvalCase]:
        """Cases with a snapshot, in file order."""
        return [c for c in self.cases if c.case_id in self.snapshots]

    def unrecorded(self) -> list[EvalCase]:
        return [c for c in self.cases if c.case_id not in self.snapshots]

    def select(
        self, *, limit: int | None = None, tag: str | None = None, ids: list[str] | None = None
    ) -> list[EvalCase]:
        cases = self.runnable()
        if ids:
            wanted = set(ids)
            cases = [c for c in cases if c.case_id in wanted]
        if tag:
            cases = [c for c in cases if tag in c.tags]
        return cases[:limit] if limit is not None else cases

    def summary(self) -> dict[str, Any]:
        return {
            "cases": len(self.cases),
            "runnable": len(self.runnable()),
            "by_action": dict(Counter(c.label.expected_action.value for c in self.cases)),
            "by_source": dict(Counter(c.label.source.value for c in self.cases)),
            "by_scenario": dict(Counter(c.label.scenario for c in self.cases)),
        }


def read_cases(path: Path = CASES_FILE) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(EvalCase.model_validate_json(line))
        except ValueError as exc:
            raise DatasetError(f"{path}:{number}: {exc}") from exc
    return cases


def write_cases(cases: list[EvalCase], path: Path = CASES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [c.model_dump_json(exclude_defaults=True) for c in cases]
    path.write_text("\n".join(lines) + "\n")


def snapshot_path(case_id: str, directory: Path = SNAPSHOTS_DIR) -> Path:
    return directory / f"{case_id}.json"


def read_snapshot(path: Path) -> Snapshot:
    return Snapshot.model_validate_json(path.read_text())


def write_snapshot(snapshot: Snapshot, directory: Path = SNAPSHOTS_DIR) -> Path:
    path = snapshot_path(snapshot.case_id, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=1, exclude_defaults=True) + "\n")
    return path


def load_dataset(cases_file: Path = CASES_FILE, snapshots_dir: Path = SNAPSHOTS_DIR) -> Dataset:
    """Load and validate the dataset. Raises `DatasetError` on any inconsistency."""
    cases = read_cases(cases_file)
    duplicates = [k for k, n in Counter(c.case_id for c in cases).items() if n > 1]
    if duplicates:
        raise DatasetError(f"duplicate case ids: {duplicates}")
    by_id = {c.case_id: c for c in cases}
    snapshots: dict[str, Snapshot] = {}
    for path in sorted(snapshots_dir.glob("*.json")) if snapshots_dir.exists() else []:
        try:
            snapshot = read_snapshot(path)
        except ValueError as exc:
            raise DatasetError(f"{path}: {exc}") from exc
        case = by_id.get(snapshot.case_id)
        if case is None or path.stem != snapshot.case_id:
            raise DatasetError(f"{path}: snapshot for unknown case {snapshot.case_id!r}")
        if not snapshot.covers(case.address):
            raise DatasetError(f"{path}: snapshot does not cover the case address on every port")
        snapshots[snapshot.case_id] = snapshot
    return Dataset(cases=cases, snapshots=snapshots)
