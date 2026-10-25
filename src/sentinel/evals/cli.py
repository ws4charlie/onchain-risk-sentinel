"""`sentinel-eval`: run, compare and gate evals.

sentinel-eval configs
sentinel-eval dataset
sentinel-eval run --config default [--cases N] [--tag smoke] [--concurrency K]
sentinel-eval compare evals/results/a.json evals/results/b.json --out evals/reports/x.md
sentinel-eval baseline evals/results/a.json --subsets smoke
sentinel-eval gate evals/results/a.json --baseline evals/baseline.json [--subset smoke]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sentinel.core.config import Settings
from sentinel.evals import compare, gate
from sentinel.evals.configs import CONFIGS, build_models, get_config
from sentinel.evals.dataset import load_dataset
from sentinel.evals.harness import RESULTS_DIR, CaseResult, read_results, run_eval, write_results
from sentinel.evals.metrics import compute_metrics, run_metrics
from sentinel.evals.recorded import offline_pattern_kb
from sentinel.observability import DEFAULT_PRICES, PriceTable

EXIT_GATE_FAILED = 1
EXIT_UNAVAILABLE = 2


def _configs(_: argparse.Namespace) -> int:
    for name, config in CONFIGS.items():
        missing = config.missing_requirements()
        status = "ready" if not missing else "unavailable: " + "; ".join(missing)
        print(f"{name:20} {status}\n{'':20} {config.models()}")
    return 0


def _dataset(_: argparse.Namespace) -> int:
    print(json.dumps(load_dataset().summary(), indent=2))
    return 0


def _progress(result: CaseResult) -> None:
    outcome = result.error or (
        f"{result.predicted_action.value if result.predicted_action else '-'} "
        f"(expected {result.expected_action.value})"
    )
    print(
        f"  {result.case_id} {result.scenario:24} {result.latency_s:7.1f}s  {outcome}", flush=True
    )


def _run(args: argparse.Namespace) -> int:
    config = get_config(args.config)
    missing = config.missing_requirements()
    if missing:
        print(f"config {config.name} cannot run here: {'; '.join(missing)}", file=sys.stderr)
        return 0 if args.skip_unavailable else EXIT_UNAVAILABLE
    dataset = load_dataset()
    cases = dataset.select(limit=args.cases, tag=args.tag, ids=args.ids)
    if not cases:
        print("no runnable cases selected", file=sys.stderr)
        return EXIT_UNAVAILABLE
    prices = PriceTable.from_file(args.prices) if args.prices else DEFAULT_PRICES
    settings = config.apply(Settings())
    models = build_models(config, settings)
    print(f"running {len(cases)} cases with {config.name}: {config.models()}", flush=True)
    results = asyncio.run(
        run_eval(
            cases,
            dataset.snapshots,
            config,
            lambda: models,
            settings,
            offline_pattern_kb(),
            concurrency=args.concurrency,
            prices=prices,
            timeout_s=args.timeout,
            progress=_progress,
        )
    )
    path = write_results(results, args.out)
    metrics = run_metrics(results)
    print(metrics.model_dump_json(indent=2, exclude={"confusion"}))
    print(f"results: {path}")
    return 0


def _compare(args: argparse.Namespace) -> int:
    runs = [read_results(p) for p in args.results]
    report = compare.render(runs, title=args.title)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"report: {args.out}")
    else:
        print(report)
    return 0


def _baseline(args: argparse.Namespace) -> int:
    results = read_results(args.results)
    baseline = gate.baseline_from_results(results, args.subsets, note=args.note)
    baseline.save(args.out)
    print(f"baseline: {args.out} (from {results.run_id})")
    return 0


def _gate(args: argparse.Namespace) -> int:
    results = read_results(args.results)
    baseline = gate.Baseline.load(args.baseline)
    cases = results.cases
    if args.subset != "all":
        cases = [c for c in cases if args.subset in c.tags]
    if results.dataset_sha256 != baseline.dataset_sha256:
        print("warning: dataset differs from the baseline's dataset", file=sys.stderr)
    same_config = results.config.name == baseline.config
    if not same_config:
        print(
            f"warning: run config {results.config.name!r} differs from baseline config "
            f"{baseline.config!r}; cost is not compared",
            file=sys.stderr,
        )
    checks = gate.check(compute_metrics(cases), baseline, args.subset, compare_cost=same_config)
    for c in checks:
        print(f"{'PASS' if c.passed else 'FAIL'}  {c.name:18} {c.detail}")
    ok = gate.passed(checks)
    print("gate passed" if ok else "gate FAILED")
    return 0 if ok else EXIT_GATE_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel-eval", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("configs", help="List model configurations and whether they can run.")
    sub.add_parser("dataset", help="Validate the dataset and print a summary.")

    run = sub.add_parser("run", help="Run the graph over the dataset.")
    run.add_argument("--config", default="default", choices=sorted(CONFIGS))
    run.add_argument("--cases", type=int, default=None, help="Run only the first N cases.")
    run.add_argument("--tag", default=None, help="Run only cases with this tag (e.g. smoke).")
    run.add_argument("--ids", nargs="*", default=None, help="Run only these case ids.")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout", type=float, default=600.0, help="Per-case timeout, seconds.")
    run.add_argument("--prices", type=Path, default=None, help="JSON price table override.")
    run.add_argument("--out", type=Path, default=RESULTS_DIR)
    run.add_argument(
        "--skip-unavailable",
        action="store_true",
        help="Exit 0 instead of 2 when keys or local models are missing.",
    )

    cmp = sub.add_parser("compare", help="Write a Markdown comparison of several runs.")
    cmp.add_argument("results", nargs="+", type=Path)
    cmp.add_argument("--out", type=Path, default=None)
    cmp.add_argument("--title", default="Model comparison")

    base = sub.add_parser("baseline", help="Write a baseline file from a run.")
    base.add_argument("results", type=Path)
    base.add_argument("--subsets", nargs="*", default=["smoke"])
    base.add_argument("--out", type=Path, default=gate.BASELINE_FILE)
    base.add_argument("--note", default="")

    g = sub.add_parser("gate", help="Fail when a run regresses against the baseline.")
    g.add_argument("results", type=Path)
    g.add_argument("--baseline", type=Path, default=gate.BASELINE_FILE)
    g.add_argument("--subset", default="all", help="'all' or a case tag such as 'smoke'.")
    return parser


COMMANDS = {
    "configs": _configs,
    "dataset": _dataset,
    "run": _run,
    "compare": _compare,
    "baseline": _baseline,
    "gate": _gate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
