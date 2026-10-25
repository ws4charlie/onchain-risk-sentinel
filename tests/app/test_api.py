"""API tests: fake ports, scripted models, in-memory checkpoints, no network."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sentinel.app.api import create_app
from tests.app.conftest import runtime_factory
from tests.fakes import CLEAN, SANCTIONED


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(runtime_factory())) as client:
        yield client


def wait_for(client: TestClient, thread_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body: dict[str, Any] = client.get(f"/cases/{thread_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"case {thread_id} still running after {timeout}s")


def start(client: TestClient, address: str, **fields: Any) -> str:
    response = client.post("/cases", json={"address": address, **fields})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "running"
    thread_id: str = body["thread_id"]
    return thread_id


def read_sse(client: TestClient, thread_id: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    with client.stream("GET", f"/cases/{thread_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        kind = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                kind = line.removeprefix("event: ")
            elif line.startswith("data: "):
                events.append((kind, json.loads(line.removeprefix("data: "))))
    return events


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok", "persistent": False}


def test_clean_case_runs_to_verdict(client: TestClient) -> None:
    thread_id = start(client, CLEAN, case_id="api-clean", amount=1.0)
    assert thread_id.startswith("api-clean-")

    body = wait_for(client, thread_id)

    assert body["status"] == "completed"
    assert body["case_id"] == "api-clean"
    assert body["verdict"]["action"] == "allow"
    assert body["review_request"] is None


def test_paused_case_is_resumed_through_review(client: TestClient) -> None:
    thread_id = start(client, SANCTIONED, case_id="api-sanctioned")
    paused = wait_for(client, thread_id)
    assert paused["status"] == "awaiting_review"
    assert paused["verdict"] is None
    assert paused["review_request"]["proposed_action"] == "block"

    response = client.post(
        f"/cases/{thread_id}/review",
        json={
            "case_id": "api-sanctioned",
            "reviewer": "analyst-1",
            "approve": False,
            "override_action": "hold_for_review",
            "note": "needs a second look",
        },
    )
    assert response.status_code == 202, response.text

    final = wait_for(client, thread_id)
    assert final["status"] == "completed"
    assert final["verdict"]["action"] == "hold_for_review"
    assert final["verdict"]["reviewed_by"] == "analyst-1"
    assert "needs a second look" in final["verdict"]["summary"]


def test_sse_stream_yields_node_events_then_terminal(client: TestClient) -> None:
    thread_id = start(client, SANCTIONED)

    events = read_sse(client, thread_id)

    kinds = [kind for kind, _ in events]
    nodes = [data["node"] for kind, data in events if kind == "node"]
    assert kinds[-1] == "paused"
    assert set(kinds[:-1]) == {"node"}
    assert {"supervisor", "sanctions", "risk_scorer", "critic"} <= set(nodes)
    sanctions = next(data for kind, data in events if data.get("node") == "sanctions")
    assert sanctions["data"]["findings"][0]["risk_signal"] == "critical"
    assert events[-1][1]["data"]["review_request"]["score"] == 100

    # A later subscriber to a finished run gets the full replay too.
    assert [k for k, _ in read_sse(client, thread_id)] == kinds


def test_sse_after_review_streams_the_resume(client: TestClient) -> None:
    thread_id = start(client, SANCTIONED, case_id="sse-review")
    wait_for(client, thread_id)
    client.post(
        f"/cases/{thread_id}/review",
        json={"case_id": "sse-review", "reviewer": "a", "approve": True},
    )

    events = read_sse(client, thread_id)

    assert [data.get("node") for kind, data in events if kind == "node"] == ["human_review"]
    assert events[-1][0] == "completed"
    assert events[-1][1]["data"]["verdict"]["action"] == "block"


def test_unknown_thread_is_404(client: TestClient) -> None:
    assert client.get("/cases/nope").status_code == 404
    assert client.get("/cases/nope/events").status_code == 404
    review = {"case_id": "x", "reviewer": "a", "approve": True}
    assert client.post("/cases/nope/review", json=review).status_code == 404


def test_review_on_completed_case_is_409(client: TestClient) -> None:
    thread_id = start(client, CLEAN, case_id="done")
    wait_for(client, thread_id)

    response = client.post(
        f"/cases/{thread_id}/review", json={"case_id": "done", "reviewer": "a", "approve": True}
    )

    assert response.status_code == 409
    assert "not awaiting review" in response.json()["detail"]


def test_review_for_another_case_is_422(client: TestClient) -> None:
    thread_id = start(client, SANCTIONED, case_id="mine")
    wait_for(client, thread_id)

    response = client.post(
        f"/cases/{thread_id}/review", json={"case_id": "other", "reviewer": "a", "approve": True}
    )

    assert response.status_code == 422
    assert wait_for(client, thread_id)["status"] == "awaiting_review"


def test_rejection_without_override_is_422(client: TestClient) -> None:
    thread_id = start(client, SANCTIONED, case_id="c1")
    wait_for(client, thread_id)

    response = client.post(
        f"/cases/{thread_id}/review", json={"case_id": "c1", "reviewer": "a", "approve": False}
    )

    assert response.status_code == 422


def test_invalid_address_is_422(client: TestClient) -> None:
    assert client.post("/cases", json={"address": "0x123"}).status_code == 422
