"""Metrics on hand-made results, the regression gate and the comparison report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sentinel.core.schemas import Action, RiskLevel
from sentinel.evals import compare, gate
from sentinel.evals.cli import main
from sentinel.evals.configs import CONFIGS
from sentinel.evals.dataset import LabelSource
from sentinel.evals.harness import CaseResult, RunResults, write_results
from sentinel.evals.metrics import compute_metrics, percentile
from sentinel.observability import TokenUsage

A, H, B = Action.ALLOW, Action.HOLD_FOR_REVIEW, Action.BLOCK


def result(
    case_id: str,
    expected: Action,
    predicted: Action | None,
    *,
    level: RiskLevel = RiskLevel.HIGH,
    floor: RiskLevel = RiskLevel.NONE,
    latency: float = 1.0,
    cost: float | None = 0.01,
    error: str | None = None,
    paused: bool = False,
    medium: int = 0,
    cited: int = 0,
    tags: list[str] | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        scenario=f"s-{expected.value}",
        label_source=LabelSource.SYNTHETIC,
        expected_action=expected,
        min_risk_level=floor,
        tags=tags or [],
        predicted_action=predicted,
        risk_level=level if predicted else None,
        paused=paused,
        latency_s=latency,
        medium_plus_findings=medium,
        medium_plus_with_evidence=cited,
        usage=TokenUsage(calls=2, input_tokens=100, output_tokens=10, cost_usd=cost),
        error=error,
    )


CASES = [
    result("ev-0001", B, B, paused=True, medium=2, cited=2, tags=["smoke"]),
    result("ev-0002", B, H, medium=1, cited=0),
    result("ev-0003", B, None, error="timeout", latency=10.0),
    result("ev-0004", H, H, floor=RiskLevel.CRITICAL, latency=2.0),
    result("ev-0005", A, A, level=RiskLevel.NONE, tags=["smoke"]),
    result("ev-0006", A, B, level=RiskLevel.MEDIUM, latency=3.0),
]


def test_metrics_on_hand_made_results() -> None:
    m = compute_metrics(CASES)
    assert m.cases == 6
    assert m.errors == 1
    assert m.action_accuracy == pytest.approx(3 / 6, abs=1e-4)
    assert m.block_recall == pytest.approx(1 / 3, abs=1e-4)
    assert m.false_block_rate == pytest.approx(1 / 2, abs=1e-4)
    assert m.pause_rate == pytest.approx(1 / 6, abs=1e-4)
    assert m.evidence_coverage == pytest.approx(2 / 3, abs=1e-4)
    # ev-0003 errored and ev-0004 is below its floor.
    assert m.risk_floor_rate == pytest.approx(4 / 6, abs=1e-4)
    assert m.latency_p50_s == 1.0
    assert m.latency_p95_s == 10.0
    assert m.tokens_per_case == 110
    assert m.cost_per_case_usd == pytest.approx(0.01)
    assert m.confusion["block"] == {"allow": 0, "hold_for_review": 1, "block": 1, "error": 1}
    assert m.confusion["allow"]["block"] == 1


def test_metrics_handle_missing_classes_and_prices() -> None:
    m = compute_metrics([result("ev-0001", H, H, cost=None)])
    assert m.block_recall is None
    assert m.false_block_rate is None
    assert m.evidence_coverage is None
    assert m.cost_per_case_usd is None
    assert compute_metrics([]).cases == 0


def test_percentile_nearest_rank() -> None:
    assert percentile([], 50) == 0.0
    assert percentile([3, 1, 2], 50) == 2
    assert percentile(list(range(1, 101)), 95) == 95


def _run(cases: list[CaseResult], name: str = "gpt-mini") -> RunResults:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    return RunResults(
        run_id=f"{name}-test",
        config=CONFIGS[name],
        started_at=now,
        finished_at=now,
        dataset_sha256="abc",
        concurrency=1,
        cases=cases,
    )


def _baseline(**tolerances: Any) -> gate.Baseline:
    good = [
        result("ev-0001", B, B, tags=["smoke"]),
        result("ev-0002", B, B),
        result("ev-0003", A, A, level=RiskLevel.NONE, tags=["smoke"]),
        result("ev-0004", A, A, level=RiskLevel.NONE),
    ]
    return gate.baseline_from_results(
        _run(good), ["smoke"], tolerances=gate.Tolerances(**tolerances)
    )


def test_gate_passes_on_equal_results() -> None:
    baseline = _baseline()
    assert set(baseline.metrics) == {"all", "smoke"}
    checks = gate.check(baseline.metrics["all"], baseline)
    assert gate.passed(checks)


@pytest.mark.parametrize(
    ("cases", "failing"),
    [
        (
            [result("1", B, H), result("2", B, B), result("3", A, A), result("4", A, A)],
            "block_recall",
        ),
        (
            [result("1", B, B), result("2", B, B), result("3", A, B), result("4", A, A)],
            "false_block_rate",
        ),
        ([result("1", B, B, cost=0.02), result("2", A, A, cost=0.02)], "cost_per_case"),
        ([result("1", B, B), result("2", A, None, error="x")], "error_rate"),
    ],
)
def test_gate_fails_on_regression(cases: list[CaseResult], failing: str) -> None:
    checks = gate.check(compute_metrics(cases), _baseline())
    failed = {c.name for c in checks if not c.passed}
    assert failing in failed


def test_gate_tolerance_allows_small_cost_rise() -> None:
    cases = [result("1", B, B, cost=0.012), result("2", A, A, cost=0.012)]
    assert gate.passed(gate.check(compute_metrics(cases), _baseline(cost_rise_ratio=0.25)))
    assert not gate.passed(gate.check(compute_metrics(cases), _baseline(cost_rise_ratio=0.1)))


def test_gate_cli_exit_codes(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    _baseline().save(baseline_path)
    good = write_results(
        _run([result("1", B, B, tags=["smoke"]), result("2", A, A, tags=["smoke"])]), tmp_path
    )
    bad = write_results(_run([result("1", B, A, tags=["smoke"])], "gpt-nano"), tmp_path)
    assert main(["gate", str(good), "--baseline", str(baseline_path), "--subset", "smoke"]) == 0
    assert main(["gate", str(bad), "--baseline", str(baseline_path), "--subset", "smoke"]) == 1


def test_baseline_cli_round_trip(tmp_path: Path) -> None:
    path = write_results(_run(CASES), tmp_path)
    out = tmp_path / "baseline.json"
    assert main(["baseline", str(path), "--subsets", "smoke", "--out", str(out)]) == 0
    loaded = gate.Baseline.load(out)
    assert loaded.metrics["smoke"].cases == 2
    assert main(["gate", str(path), "--baseline", str(out)]) == 0


def test_compare_report_and_recommendation(tmp_path: Path) -> None:
    cheap_good = _run([result("1", B, B, cost=0.001), result("2", A, A, cost=0.001)], "gpt-nano")
    pricey_good = _run([result("1", B, B, cost=0.01), result("2", A, A, cost=0.01)], "gpt-mini")
    weak = _run([result("1", B, H, cost=0.0), result("2", A, A, cost=0.0)], "ollama-qwen")
    report = compare.render([pricey_good, cheap_good, weak])
    assert "| `gpt-nano` | 2 | 0 | 100% | 100% | 0%" in report
    assert "## Not run" in report and "`default`" in report
    assert "Rule-based pick: `gpt-nano`" in report

    a = write_results(cheap_good, tmp_path)
    out = tmp_path / "report.md"
    assert main(["compare", str(a), "--out", str(out)]) == 0
    assert out.read_text().startswith("# Model comparison")


def test_gate_skips_cost_across_configs() -> None:
    pricier = [result("1", B, B, cost=1.0), result("2", A, A, cost=1.0)]
    checks = gate.check(compute_metrics(pricier), _baseline(), compare_cost=False)
    assert gate.passed(checks)
    assert (
        next(c for c in checks if c.name == "cost_per_case").detail == "different config; skipped"
    )
