"""Comparison report: one Markdown page across several eval runs.

The recommendation is rule-based so it is reproducible: among runs with the
best block recall (within 0.05) and an error rate under 5%, prefer the highest
action accuracy, then the lowest cost per case, then the lowest p95 latency.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sentinel.evals.configs import CONFIGS
from sentinel.evals.harness import RunResults
from sentinel.evals.metrics import ACTIONS, ERROR, Metrics, per_scenario_accuracy, run_metrics


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def recommend(runs: Sequence[tuple[RunResults, Metrics]]) -> tuple[RunResults, str] | None:
    usable = [(r, m) for r, m in runs if m.cases and m.errors / m.cases < 0.05]
    if not usable:
        return None
    best_recall = max(m.block_recall or 0.0 for _, m in usable)
    candidates = [(r, m) for r, m in usable if (m.block_recall or 0.0) >= best_recall - 0.05]
    candidates.sort(
        key=lambda rm: (
            -rm[1].action_accuracy,
            rm[1].cost_per_case_usd if rm[1].cost_per_case_usd is not None else float("inf"),
            rm[1].latency_p95_s,
        )
    )
    run, m = candidates[0]
    reason = (
        f"`{run.config.name}` has block recall {_pct(m.block_recall)} (best is "
        f"{_pct(best_recall)}), action accuracy {_pct(m.action_accuracy)}, cost "
        f"{_usd(m.cost_per_case_usd)} per case and p95 latency {m.latency_p95_s:.1f}s."
    )
    return run, reason


def render(runs: Sequence[RunResults], *, title: str = "Model comparison") -> str:
    measured = [(r, run_metrics(r)) for r in runs]
    lines = [
        f"# {title}",
        "",
        f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by `sentinel-eval compare`. "
        "Metric definitions: `src/sentinel/evals/metrics.py` and `evals/README.md`.",
        "",
        "## Results",
        "",
        "| Config | Cases | Errors | Accuracy | Block recall | False block | Risk floor "
        "| Pause | Evidence cov. | p50 s | p95 s | Tokens/case | Cost/case |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r, m in measured:
        lines.append(
            f"| `{r.config.name}` | {m.cases} | {m.errors} | {_pct(m.action_accuracy)} "
            f"| {_pct(m.block_recall)} | {_pct(m.false_block_rate)} | {_pct(m.risk_floor_rate)} "
            f"| {_pct(m.pause_rate)} | {_pct(m.evidence_coverage)} | {m.latency_p50_s:.1f} "
            f"| {m.latency_p95_s:.1f} | {m.tokens_per_case:,.0f} | {_usd(m.cost_per_case_usd)} |"
        )

    lines += [
        "",
        "## Configurations",
        "",
        "| Config | Run id | Models (role: model) |",
        "|---|---|---|",
    ]
    for r, _ in measured:
        models = ", ".join(f"{role}: `{model}`" for role, model in r.config.models().items())
        lines.append(f"| `{r.config.name}` | `{r.run_id}` | {models} |")

    ran = {r.config.name for r in runs}
    skipped = [c for name, c in CONFIGS.items() if name not in ran]
    if skipped:
        lines += ["", "## Not run", ""]
        for c in skipped:
            reasons = c.missing_requirements() or ["not run in this comparison"]
            lines.append(f"- `{c.name}` ({c.description}): {'; '.join(reasons)}.")

    lines += ["", "## Confusion matrices (rows: expected, columns: predicted)", ""]
    for r, m in measured:
        lines += [
            f"### `{r.config.name}`",
            "",
            "| expected \\ predicted | " + " | ".join([*ACTIONS, ERROR]) + " |",
        ]
        lines.append("|---" * (len(ACTIONS) + 2) + "|")
        for expected, row in m.confusion.items():
            lines.append(
                f"| {expected} | " + " | ".join(str(row[k]) for k in [*ACTIONS, ERROR]) + " |"
            )
        lines.append("")

    scenarios = sorted({c.scenario for r in runs for c in r.cases})
    per_run = [per_scenario_accuracy(r.cases) for r in runs]
    lines += [
        "## Accuracy by scenario",
        "",
        "| Scenario | " + " | ".join(f"`{r.config.name}`" for r in runs) + " |",
        "|---" * (len(runs) + 1) + "|",
    ]
    for s in scenarios:
        cells = []
        for table in per_run:
            right, total = table.get(s, (0, 0))
            cells.append(f"{right}/{total}" if total else "-")
        lines.append(f"| {s} | " + " | ".join(cells) + " |")

    lines += ["", "## Recommendation", ""]
    pick = recommend(measured)
    lines.append(
        f"Rule-based pick: {pick[1]}"
        if pick
        else "No run qualifies (all runs error on >= 5% of cases)."
    )
    lines.append("")
    return "\n".join(lines)
