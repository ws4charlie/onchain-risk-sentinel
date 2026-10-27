"""Metrics over one eval run.

Definitions (a case with an error counts as wrong, never as right):
- action_accuracy: predicted action == expected action, over all cases.
- block_recall: share of must-block cases (expected block) predicted block.
- false_block_rate: share of benign cases (expected allow) predicted block.
- risk_floor_rate: share of cases whose predicted risk level is >= the label's minimum.
- pause_rate: share of cases that paused for human review.
- evidence_coverage: share of medium+ findings that cite at least one evidence item.
- latency p50/p95 (seconds per case), tokens and USD cost per case (mean).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from sentinel.core.schemas import Action
from sentinel.evals.dataset import RISK_ORDER
from sentinel.evals.harness import CaseResult, RunResults

ERROR = "error"
ACTIONS = [a.value for a in Action]


class Metrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: int
    errors: int
    action_accuracy: float
    block_recall: float | None
    false_block_rate: float | None
    risk_floor_rate: float
    pause_rate: float
    evidence_coverage: float | None
    latency_p50_s: float
    latency_p95_s: float
    tokens_per_case: float
    cost_per_case_usd: float | None
    total_cost_usd: float | None
    unrecorded_lookup_rate: float
    confusion: dict[str, dict[str, int]]


def _ratio(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile (q in [0, 100]); 0.0 for no values."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q / 100 * len(ordered)))
    return ordered[rank - 1]


def confusion_matrix(cases: Sequence[CaseResult]) -> dict[str, dict[str, int]]:
    """expected action -> predicted action (or 'error') -> count."""
    matrix = {e: dict.fromkeys([*ACTIONS, ERROR], 0) for e in ACTIONS}
    for c in cases:
        predicted = c.predicted_action.value if c.predicted_action and not c.error else ERROR
        matrix[c.expected_action.value][predicted] += 1
    return matrix


def _predicted(c: CaseResult) -> Action | None:
    return None if c.error else c.predicted_action


def compute_metrics(cases: Sequence[CaseResult]) -> Metrics:
    n = len(cases)
    must_block = [c for c in cases if c.expected_action is Action.BLOCK]
    benign = [c for c in cases if c.expected_action is Action.ALLOW]
    medium = sum(c.medium_plus_findings for c in cases)
    costs = [c.usage.cost_usd for c in cases]
    total_cost = None if any(x is None for x in costs) else sum(x for x in costs if x is not None)
    return Metrics(
        cases=n,
        errors=sum(1 for c in cases if c.error),
        action_accuracy=_ratio(sum(1 for c in cases if _predicted(c) == c.expected_action), n)
        or 0.0,
        block_recall=_ratio(
            sum(1 for c in must_block if _predicted(c) is Action.BLOCK), len(must_block)
        ),
        false_block_rate=_ratio(
            sum(1 for c in benign if _predicted(c) is Action.BLOCK), len(benign)
        ),
        risk_floor_rate=_ratio(
            sum(
                1
                for c in cases
                if not c.error
                and c.risk_level is not None
                and RISK_ORDER[c.risk_level] >= RISK_ORDER[c.min_risk_level]
            ),
            n,
        )
        or 0.0,
        pause_rate=_ratio(sum(1 for c in cases if c.paused), n) or 0.0,
        evidence_coverage=_ratio(sum(c.medium_plus_with_evidence for c in cases), medium),
        latency_p50_s=round(percentile([c.latency_s for c in cases], 50), 3),
        latency_p95_s=round(percentile([c.latency_s for c in cases], 95), 3),
        tokens_per_case=round(sum(c.usage.total_tokens for c in cases) / n, 1) if n else 0.0,
        cost_per_case_usd=round(total_cost / n, 6) if total_cost is not None and n else None,
        total_cost_usd=round(total_cost, 6) if total_cost is not None else None,
        unrecorded_lookup_rate=_ratio(sum(1 for c in cases if c.unrecorded_lookups), n) or 0.0,
        confusion=confusion_matrix(cases),
    )


def run_metrics(results: RunResults) -> Metrics:
    return compute_metrics(results.cases)


def per_scenario_accuracy(cases: Sequence[CaseResult]) -> dict[str, tuple[int, int]]:
    """scenario -> (correct, total)."""
    table: dict[str, tuple[int, int]] = {}
    for c in cases:
        right, total = table.get(c.scenario, (0, 0))
        table[c.scenario] = (right + (_predicted(c) == c.expected_action), total + 1)
    return dict(sorted(table.items()))
