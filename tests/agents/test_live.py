"""Runs the real graph with the models configured in Settings. Skipped unless `-m live`."""

from __future__ import annotations

import pytest

from sentinel.agents import Models, build_graph, resume_case, run_case
from sentinel.core.config import Settings
from sentinel.core.schemas import Action, Case, ReviewDecision
from tests.fakes import SANCTIONED, fake_ports


@pytest.mark.live
async def test_live_models_triage_sanctioned_fixture() -> None:
    settings = Settings()
    graph = build_graph(fake_ports(), Models.from_settings(settings), settings)
    case = Case(case_id="live-1", address=SANCTIONED, amount=2.0)

    run = await run_case(graph, case)

    # The sanctions rule is deterministic, so a working model stack must end in review.
    assert run.review_request is not None
    assert run.review_request["proposed_action"] == "block"
    final = await resume_case(
        graph, run.thread_id, ReviewDecision(case_id="live-1", reviewer="live", approve=True)
    )
    assert final.verdict is not None
    assert final.verdict.action is Action.BLOCK
