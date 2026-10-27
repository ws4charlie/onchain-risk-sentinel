"""Dataset loading and validation, on the committed dataset and on broken copies."""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from sentinel.core.schemas import Action
from sentinel.evals import synthetic
from sentinel.evals.dataset import (
    CASES_FILE,
    SNAPSHOTS_DIR,
    DatasetError,
    LabelSource,
    load_dataset,
    read_snapshot,
    snapshot_path,
)


def test_committed_dataset_is_valid_and_balanced() -> None:
    dataset = load_dataset()
    assert 60 <= len(dataset.cases) <= 100
    assert len(dataset.runnable()) >= 60
    actions = Counter(c.label.expected_action for c in dataset.cases)
    assert min(actions.values()) >= len(dataset.cases) // 5
    smoke = dataset.select(tag="smoke")
    assert {c.label.expected_action for c in smoke} == set(Action)


def test_case_ids_do_not_leak_labels() -> None:
    for case in load_dataset().cases:
        assert case.case_id.startswith("ev-")
        assert case.label.scenario not in case.to_case().model_dump_json()


def test_committed_dataset_matches_the_generator(tmp_path: Path) -> None:
    """cases.jsonl and synthetic snapshots are exactly what the seeded generator writes."""
    synthetic.write(tmp_path / "cases.jsonl", tmp_path / "snapshots")
    assert (tmp_path / "cases.jsonl").read_text() == CASES_FILE.read_text()
    for path in (tmp_path / "snapshots").glob("*.json"):
        assert path.read_text() == (SNAPSHOTS_DIR / path.name).read_text()


def test_real_cases_have_no_synthetic_snapshot() -> None:
    dataset = load_dataset()
    for case in dataset.cases:
        snapshot = dataset.snapshots.get(case.case_id)
        if case.label.source is not LabelSource.SYNTHETIC and snapshot is not None:
            assert snapshot.origin.value == "recorded"


def test_select_limits_and_filters() -> None:
    dataset = load_dataset()
    first = dataset.runnable()[:3]
    assert dataset.select(limit=3) == first
    assert dataset.select(ids=[first[1].case_id]) == [first[1]]


@pytest.fixture
def copy(tmp_path: Path) -> tuple[Path, Path]:
    cases = tmp_path / "cases.jsonl"
    snapshots = tmp_path / "snapshots"
    shutil.copy(CASES_FILE, cases)
    shutil.copytree(SNAPSHOTS_DIR, snapshots)
    return cases, snapshots


def test_duplicate_case_ids_are_rejected(copy: tuple[Path, Path]) -> None:
    cases, snapshots = copy
    first = cases.read_text().splitlines()[0]
    cases.write_text(cases.read_text() + first + "\n")
    with pytest.raises(DatasetError, match="duplicate"):
        load_dataset(cases, snapshots)


def test_invalid_line_reports_its_position(copy: tuple[Path, Path]) -> None:
    cases, snapshots = copy
    cases.write_text(cases.read_text() + '{"case_id": "ev-9999", "address": "0x12"}\n')
    with pytest.raises(DatasetError, match=r"cases.jsonl:88"):
        load_dataset(cases, snapshots)


def test_snapshot_for_unknown_case_is_rejected(copy: tuple[Path, Path]) -> None:
    cases, snapshots = copy
    shutil.copy(snapshot_path("ev-0001", snapshots), snapshots / "ev-0999.json")
    with pytest.raises(DatasetError, match="unknown case"):
        load_dataset(cases, snapshots)


def test_snapshot_must_cover_the_case_address(copy: tuple[Path, Path]) -> None:
    cases, snapshots = copy
    path = snapshot_path("ev-0001", snapshots)
    snapshot = read_snapshot(path)
    broken = snapshot.model_copy(update={"sanctions": {}})
    path.write_text(broken.model_dump_json())
    with pytest.raises(DatasetError, match="does not cover"):
        load_dataset(cases, snapshots)


def test_generator_is_deterministic() -> None:
    a_cases, a_snaps = synthetic.build()
    b_cases, b_snaps = synthetic.build()
    assert a_cases == b_cases
    assert a_snaps == b_snaps
    assert synthetic.build(seed=1)[0] != a_cases
