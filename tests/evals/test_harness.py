"""Harness run over 3 dataset cases with the scripted fake models (no network, no LLM)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.agents import Models
from sentinel.core.config import Settings
from sentinel.core.schemas import Action
from sentinel.evals.configs import CONFIGS
from sentinel.evals.dataset import load_dataset
from sentinel.evals.harness import read_results, run_eval, run_one, write_results
from sentinel.evals.metrics import run_metrics
from sentinel.evals.recorded import offline_pattern_kb
from tests.agents.conftest import make_models
from tests.agents.fake_llm import ScriptedChatModel


def _pick(scenarios: list[str]) -> list[str]:
    dataset = load_dataset()
    return [next(c.case_id for c in dataset.runnable() if c.label.scenario == s) for s in scenarios]


async def test_harness_runs_three_cases_with_fake_models(tmp_path: Path) -> None:
    dataset = load_dataset()
    ids = _pick(["sanctioned_direct", "drainer_report", "benign_fresh_small"])
    cases = dataset.select(ids=ids)
    seen: list[str] = []

    results = await run_eval(
        cases,
        dataset.snapshots,
        CONFIGS["gpt-mini"],
        make_models,
        Settings(_env_file=None),  # type: ignore[call-arg]
        offline_pattern_kb(),
        concurrency=2,
        progress=lambda r: seen.append(r.case_id),
    )

    assert sorted(seen) == sorted(ids)
    by_id = {r.case_id: r for r in results.cases}
    sanctioned, drainer, benign = (by_id[i] for i in ids)

    assert sanctioned.error is None
    assert sanctioned.paused and sanctioned.predicted_action is Action.BLOCK
    assert sanctioned.score == 100
    assert drainer.paused and drainer.predicted_action is Action.BLOCK
    assert benign.predicted_action is Action.ALLOW and not benign.paused

    for r in results.cases:
        assert r.findings == 4
        assert r.evidence > 0
        assert r.latency_s > 0
        assert r.unrecorded_lookups == []
        # Scripted models report no token counts, but every call is attributed to a role.
        assert set(r.usage_by_role) == {"supervisor", "specialist", "scorer", "critic"}
        assert r.usage_by_role["specialist"].calls >= 8  # 4 specialists x 2 ReAct turns
        assert r.usage.cost_usd is None  # the scripted model has no price

    metrics = run_metrics(results)
    assert metrics.action_accuracy == 1.0
    assert metrics.block_recall == 1.0
    assert metrics.false_block_rate == 0.0
    assert metrics.pause_rate == pytest.approx(2 / 3, abs=1e-4)
    assert metrics.evidence_coverage == 1.0

    path = write_results(results, tmp_path)
    assert read_results(path) == results


async def test_failing_model_is_recorded_as_case_error() -> None:
    dataset = load_dataset()
    case = dataset.select(ids=_pick(["benign_fresh_small"]))[0]

    def broken(*_: object) -> object:
        raise RuntimeError("provider down")

    models = make_models()
    models = Models(
        supervisor=ScriptedChatModel(policy=broken),  # type: ignore[arg-type]
        specialist=models.specialist,
        scorer=models.scorer,
        critic=models.critic,
    )
    result = await run_one(
        case,
        dataset.snapshots[case.case_id],
        models,
        Settings(_env_file=None),  # type: ignore[call-arg]
        offline_pattern_kb(),
    )
    assert result.error is not None and "provider down" in result.error
    assert result.predicted_action is None
