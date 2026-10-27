"""Integration: a case paused in one runtime is resumed by another through Postgres."""

from __future__ import annotations

import io
import json
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import pytest
from fastapi.testclient import TestClient

from sentinel.app.api import create_app
from sentinel.app.cli import EXIT_AWAITING_REVIEW, EXIT_OK, main
from sentinel.app.runner import CaseStatus, new_thread_id, read_case, stream_case, stream_resume
from sentinel.app.wiring import Runtime
from sentinel.core.schemas import Action, Case, ReviewDecision
from tests.app.conftest import DATABASE_URL, app_settings, requires_database, runtime_factory
from tests.fakes import SANCTIONED

pytestmark = [pytest.mark.integration, requires_database]


def postgres_factory() -> Callable[[], AbstractAsyncContextManager[Runtime]]:
    return runtime_factory(settings=app_settings(database_url=DATABASE_URL))


async def test_resume_with_a_new_graph_and_pool() -> None:
    factory = postgres_factory()
    case = Case(case_id="pg-graph", address=SANCTIONED)
    thread_id = new_thread_id(case.case_id)

    async with factory() as first:
        assert first.persistent
        events = [e async for e in stream_case(first, case, thread_id)]
        assert events[-1].kind == "paused"

    async with factory() as second:
        state = await read_case(second, thread_id)
        assert state is not None and state.status is CaseStatus.AWAITING_REVIEW
        decision = ReviewDecision(case_id=case.case_id, reviewer="pg", approve=True)
        events = [e async for e in stream_resume(second, thread_id, decision)]
        final = await read_case(second, thread_id)

    assert events[-1].kind == "completed"
    assert final is not None and final.verdict is not None
    assert final.verdict.action is Action.BLOCK
    assert final.verdict.reviewed_by == "pg"


def test_cli_resume_from_a_separate_invocation() -> None:
    out = io.StringIO()
    code = main(
        ["triage", SANCTIONED, "--case-id", "pg-cli", "--no-review"],
        runtime_factory=postgres_factory(),
        out=out,
        err=io.StringIO(),
    )
    assert code == EXIT_AWAITING_REVIEW
    thread_id = out.getvalue().strip()

    out = io.StringIO()
    code = main(
        ["resume", thread_id, "--override", "hold_for_review", "--json"],
        runtime_factory=postgres_factory(),
        out=out,
        err=io.StringIO(),
    )

    assert code == EXIT_OK
    assert json.loads(out.getvalue())["verdict"]["action"] == "hold_for_review"


def _settle(client: TestClient, thread_id: str) -> dict[str, object]:
    for _ in range(500):
        body: dict[str, object] = client.get(f"/cases/{thread_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("case did not settle")


def test_api_review_after_restart() -> None:
    with TestClient(create_app(postgres_factory())) as client:
        assert client.get("/healthz").json()["persistent"] is True
        response = client.post("/cases", json={"case_id": "pg-api", "address": SANCTIONED})
        thread_id = response.json()["thread_id"]
        assert _settle(client, thread_id)["status"] == "awaiting_review"

    with TestClient(create_app(postgres_factory())) as client:  # a fresh process
        assert client.get(f"/cases/{thread_id}").json()["status"] == "awaiting_review"
        review = {"case_id": "pg-api", "reviewer": "night-shift", "approve": True}
        assert client.post(f"/cases/{thread_id}/review", json=review).status_code == 202
        final = _settle(client, thread_id)

    assert final["status"] == "completed"
    assert final["verdict"]["reviewed_by"] == "night-shift"  # type: ignore[index]
