"""Eval harness: run labeled cases through the triage graph and record what happened.

Each case runs on its own `RecordedPorts`, its own in-memory checkpointer and
its own `UsageCallbackHandler`. A case that pauses for human review is resumed
with an "approve" decision (the pause itself is recorded). Failures and timeouts
are recorded per case and never stop the run.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel, ConfigDict, Field

from sentinel.agents import Models, build_graph, resume_case, run_case
from sentinel.agents.graph import checkpoint_serde
from sentinel.core.config import Settings
from sentinel.core.ports import PatternKnowledgeBase
from sentinel.core.schemas import Action, ReviewDecision, RiskLevel, Verdict
from sentinel.evals.configs import ModelConfig
from sentinel.evals.dataset import CASES_FILE, EVALS_DIR, EvalCase, LabelSource, Snapshot
from sentinel.evals.recorded import RecordedPorts
from sentinel.observability import DEFAULT_PRICES, PriceTable, TokenUsage, UsageCallbackHandler

RESULTS_DIR = EVALS_DIR / "results"
REVIEWER = "eval-harness"
MEDIUM_PLUS = frozenset({RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL})

ModelsFactory = Callable[[], Models]


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    scenario: str
    label_source: LabelSource
    expected_action: Action
    min_risk_level: RiskLevel
    tags: list[str] = Field(default_factory=list)
    predicted_action: Action | None = None
    risk_level: RiskLevel | None = None
    score: int | None = None
    findings: int = 0
    evidence: int = 0
    medium_plus_findings: int = 0
    medium_plus_with_evidence: int = 0
    paused: bool = False
    latency_s: float = 0.0
    usage_by_role: dict[str, TokenUsage] = Field(default_factory=dict)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    unrecorded_lookups: list[str] = Field(default_factory=list)
    error: str | None = None


class RunResults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    config: ModelConfig
    started_at: datetime
    finished_at: datetime
    dataset_sha256: str = Field(description="Hash of cases.jsonl, to compare like with like.")
    concurrency: int
    prices: dict[str, str] = Field(
        default_factory=dict, description="Model -> price source and date used for cost."
    )
    cases: list[CaseResult]


def dataset_hash(path: Path = CASES_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _fill_verdict(result: CaseResult, verdict: Verdict) -> None:
    result.predicted_action = verdict.action
    result.risk_level = verdict.risk_level
    result.score = verdict.score
    result.findings = len(verdict.findings)
    result.evidence = sum(len(f.evidence) for f in verdict.findings)
    medium = [f for f in verdict.findings if f.risk_signal in MEDIUM_PLUS]
    result.medium_plus_findings = len(medium)
    result.medium_plus_with_evidence = sum(1 for f in medium if f.evidence)


async def run_one(
    case: EvalCase,
    snapshot: Snapshot,
    models: Models,
    settings: Settings,
    patterns: PatternKnowledgeBase,
    *,
    prices: PriceTable = DEFAULT_PRICES,
    timeout_s: float = 600.0,
) -> CaseResult:
    label = case.label
    result = CaseResult(
        case_id=case.case_id,
        scenario=label.scenario,
        label_source=label.source,
        expected_action=label.expected_action,
        min_risk_level=label.min_risk_level,
        tags=case.tags,
    )
    ports = RecordedPorts(snapshot, patterns)
    usage = UsageCallbackHandler(prices)
    graph = build_graph(
        ports, models, settings, InMemorySaver(serde=checkpoint_serde())
    ).with_config(callbacks=[usage])

    async def triage() -> Verdict | None:
        run = await run_case(graph, case.to_case())
        if run.review_request is not None:
            result.paused = True
            decision = ReviewDecision(case_id=case.case_id, reviewer=REVIEWER, approve=True)
            run = await resume_case(graph, run.thread_id, decision)
        return run.verdict

    started = time.perf_counter()
    try:
        verdict = await asyncio.wait_for(triage(), timeout_s)
        if verdict is None:
            result.error = "run ended without a verdict"
        else:
            _fill_verdict(result, verdict)
    except TimeoutError:
        result.error = f"timeout after {timeout_s:.0f}s"
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    result.latency_s = round(time.perf_counter() - started, 3)
    result.usage_by_role = usage.by_role()
    result.usage = usage.total()
    result.unrecorded_lookups = sorted(set(ports.misses))
    return result


async def run_eval(
    cases: Sequence[EvalCase],
    snapshots: dict[str, Snapshot],
    config: ModelConfig,
    models_factory: ModelsFactory,
    settings: Settings,
    patterns: PatternKnowledgeBase,
    *,
    concurrency: int = 4,
    prices: PriceTable = DEFAULT_PRICES,
    timeout_s: float = 600.0,
    cases_file: Path = CASES_FILE,
    progress: Callable[[CaseResult], None] | None = None,
) -> RunResults:
    """Run `cases` (each must have a snapshot) with at most `concurrency` in flight."""
    started_at = datetime.now(UTC)
    settings = config.apply(settings)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(case: EvalCase) -> CaseResult:
        async with semaphore:
            result = await run_one(
                case,
                snapshots[case.case_id],
                models_factory(),
                settings,
                patterns,
                prices=prices,
                timeout_s=timeout_s,
            )
        if progress:
            progress(result)
        return result

    results = await asyncio.gather(*(one(c) for c in cases))
    used: dict[str, str] = {}
    for model in set(config.models().values()):
        price = prices.lookup(model)
        used[model] = f"{price.source} ({price.checked_on})" if price else "no price"
    return RunResults(
        run_id=f"{config.name}-{started_at:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}",
        config=config,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        dataset_sha256=dataset_hash(cases_file),
        concurrency=concurrency,
        prices=used,
        cases=list(results),
    )


def write_results(results: RunResults, directory: Path = RESULTS_DIR) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{results.run_id}.json"
    path.write_text(results.model_dump_json(indent=1) + "\n")
    return path


def read_results(path: Path) -> RunResults:
    return RunResults.model_validate_json(path.read_text())
