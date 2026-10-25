"""Regression gate: compare a run's metrics with a committed baseline.

The baseline file (`evals/baseline.json`) holds reference metrics per subset
(all cases, and each tag such as `smoke` that CI runs) and the tolerances. The gate fails when:
- block recall drops by more than `block_recall_drop`,
- false-block rate rises by more than `false_block_rise`,
- cost per case rises by more than `cost_rise_ratio` (relative),
- action accuracy drops by more than `accuracy_drop`,
- the error rate rises by more than `error_rate_rise`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from sentinel.evals.dataset import EVALS_DIR
from sentinel.evals.harness import RunResults
from sentinel.evals.metrics import Metrics, compute_metrics

BASELINE_FILE = EVALS_DIR / "baseline.json"


class Tolerances(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block_recall_drop: float = Field(default=0.0, ge=0)
    false_block_rise: float = Field(default=0.0, ge=0)
    cost_rise_ratio: float = Field(default=0.25, ge=0)
    accuracy_drop: float = Field(default=0.10, ge=0)
    error_rate_rise: float = Field(default=0.02, ge=0, le=1)


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    config: str
    dataset_sha256: str
    note: str = ""
    tolerances: Tolerances = Field(default_factory=Tolerances)
    metrics: dict[str, Metrics] = Field(
        description="Subset name ('all' or a case tag such as 'smoke') -> reference metrics."
    )

    @classmethod
    def load(cls, path: Path = BASELINE_FILE) -> Self:
        return cls.model_validate_json(path.read_text())

    def save(self, path: Path = BASELINE_FILE) -> None:
        path.write_text(self.model_dump_json(indent=1) + "\n")


class GateCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    passed: bool
    detail: str


def check(
    current: Metrics, baseline: Baseline, subset: str = "all", *, compare_cost: bool = True
) -> list[GateCheck]:
    """Run every check. Pass `compare_cost=False` when the run used another model config
    than the baseline: cost is only comparable within one configuration."""
    if subset not in baseline.metrics:
        raise KeyError(f"baseline has no metrics for subset {subset!r}")
    base, tol = baseline.metrics[subset], baseline.tolerances
    checks: list[GateCheck] = []

    def compare(name: str, now: float | None, ref: float | None, limit: float, lower: bool) -> None:
        """lower=True: the metric must not fall more than `limit` below the reference."""
        if now is None or ref is None:
            checks.append(GateCheck(name=name, passed=True, detail="not measured; skipped"))
            return
        delta = now - ref
        passed = delta >= -limit - 1e-9 if lower else delta <= limit + 1e-9
        checks.append(
            GateCheck(
                name=name,
                passed=passed,
                detail=f"{now:.4f} vs baseline {ref:.4f} (delta {delta:+.4f}, tolerance {limit})",
            )
        )

    compare("block_recall", current.block_recall, base.block_recall, tol.block_recall_drop, True)
    compare(
        "false_block_rate",
        current.false_block_rate,
        base.false_block_rate,
        tol.false_block_rise,
        False,
    )
    compare(
        "action_accuracy", current.action_accuracy, base.action_accuracy, tol.accuracy_drop, True
    )

    now_cost, ref_cost = current.cost_per_case_usd, base.cost_per_case_usd
    if not compare_cost:
        checks.append(
            GateCheck(name="cost_per_case", passed=True, detail="different config; skipped")
        )
    elif now_cost is None or ref_cost is None:
        checks.append(GateCheck(name="cost_per_case", passed=True, detail="not priced; skipped"))
    else:
        limit = ref_cost * (1 + tol.cost_rise_ratio)
        checks.append(
            GateCheck(
                name="cost_per_case",
                passed=now_cost <= limit + 1e-12,
                detail=f"${now_cost:.6f} vs limit ${limit:.6f} "
                f"(baseline ${ref_cost:.6f} + {tol.cost_rise_ratio:.0%})",
            )
        )

    compare(
        "error_rate",
        current.errors / current.cases if current.cases else 1.0,
        base.errors / base.cases if base.cases else 0.0,
        tol.error_rate_rise,
        False,
    )
    return checks


def passed(checks: list[GateCheck]) -> bool:
    return all(c.passed for c in checks)


def baseline_from_results(
    results: RunResults,
    subsets: list[str],
    *,
    tolerances: Tolerances | None = None,
    note: str = "",
) -> Baseline:
    """Build a baseline from a run: metrics over all cases plus each tagged subset."""
    metrics = {"all": compute_metrics(results.cases)}
    for tag in subsets:
        tagged = [c for c in results.cases if tag in c.tags]
        if not tagged:
            raise ValueError(f"no case in {results.run_id} has tag {tag!r}")
        metrics[tag] = compute_metrics(tagged)
    return Baseline(
        run_id=results.run_id,
        config=results.config.name,
        dataset_sha256=results.dataset_sha256,
        note=note,
        tolerances=tolerances or Tolerances(),
        metrics=metrics,
    )
