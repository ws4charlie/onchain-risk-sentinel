"""CLI tests: `main()` with an injected runtime, scripted prompts and captured output."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from sentinel.app.cli import EXIT_AWAITING_REVIEW, EXIT_ERROR, EXIT_OK, build_parser, main
from sentinel.app.wiring import Runtime
from tests.app.conftest import runtime_factory
from tests.fakes import CLEAN, SANCTIONED


class Result:
    def __init__(self, code: int, out: str, err: str) -> None:
        self.code = code
        self.out = out
        self.err = err

    def json(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.out)
        return data


def run_cli(
    *argv: str,
    factory: Callable[[], AbstractAsyncContextManager[Runtime]] | None = None,
    answers: list[str] | None = None,
) -> Result:
    replies: Iterator[str] = iter(answers or [])

    def prompt(message: str) -> str:
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        runtime_factory=factory or runtime_factory(),
        prompt=prompt,
        out=out,
        err=err,
    )
    return Result(code, out.getvalue(), err.getvalue())


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["triage", "--help"])
    assert exc.value.code == 0
    assert "SENTINEL_DATABASE_URL" in capsys.readouterr().out


def test_triage_clean_json_output() -> None:
    result = run_cli("triage", CLEAN, "--case-id", "cli-1", "--amount", "2", "--json")

    assert result.code == EXIT_OK
    body = result.json()
    assert body["status"] == "completed"
    assert body["case_id"] == "cli-1"
    assert body["verdict"]["action"] == "allow"
    assert "[supervisor]" in result.err
    assert "[sanctions]" in result.err


def test_triage_table_output() -> None:
    result = run_cli("triage", CLEAN, "--case-id", "cli-table")

    assert result.code == EXIT_OK
    assert "Action       allow" in result.out
    assert "Findings" in result.out
    assert "chain_tracer" in result.out


def test_no_review_prints_thread_id_and_exits_distinctly() -> None:
    result = run_cli("triage", SANCTIONED, "--case-id", "cli-2", "--no-review")

    assert result.code == EXIT_AWAITING_REVIEW
    thread_id = result.out.strip()
    assert thread_id.startswith("cli-2-")
    assert f"sentinel resume {thread_id}" in result.err
    assert "SENTINEL_DATABASE_URL" in result.err  # in-memory checkpoints warning


def test_no_review_json_includes_review_request() -> None:
    result = run_cli("triage", SANCTIONED, "--no-review", "--json")

    assert result.code == EXIT_AWAITING_REVIEW
    body = result.json()
    assert body["status"] == "awaiting_review"
    assert body["review_request"]["proposed_action"] == "block"


def test_interactive_approve() -> None:
    result = run_cli(
        "triage", SANCTIONED, "--reviewer", "ana", "--json", answers=["a", "confirmed"]
    )

    assert result.code == EXIT_OK
    verdict = result.json()["verdict"]
    assert verdict["action"] == "block"
    assert verdict["reviewed_by"] == "ana"
    assert "Reviewer note: confirmed" in verdict["summary"]
    assert "Human review required" in result.err


def test_interactive_override_retries_bad_input() -> None:
    answers = ["maybe", "o", "nuke", "o", "hold_for_review", ""]
    result = run_cli("triage", SANCTIONED, "--json", answers=answers)

    assert result.code == EXIT_OK
    assert result.json()["verdict"]["action"] == "hold_for_review"
    assert "please answer" in result.err
    assert "unknown action 'nuke'" in result.err


def test_end_of_input_behaves_like_no_review() -> None:
    result = run_cli("triage", SANCTIONED, answers=[])

    assert result.code == EXIT_AWAITING_REVIEW


def test_resume_in_a_new_runtime(saver: InMemorySaver) -> None:
    factory = runtime_factory(checkpointer=saver)
    paused = run_cli("triage", SANCTIONED, "--case-id", "cli-3", "--no-review", factory=factory)
    thread_id = paused.out.strip()

    result = run_cli(
        "resume",
        thread_id,
        "--override",
        "allow",
        "--note",
        "known counterparty",
        "--reviewer",
        "lead",
        "--json",
        factory=factory,
    )

    assert result.code == EXIT_OK
    verdict = result.json()["verdict"]
    assert verdict["action"] == "allow"
    assert verdict["reviewed_by"] == "lead"


def test_resume_twice_fails(saver: InMemorySaver) -> None:
    factory = runtime_factory(checkpointer=saver)
    thread_id = run_cli("triage", SANCTIONED, "--no-review", factory=factory).out.strip()
    assert run_cli("resume", thread_id, "--approve", factory=factory).code == EXIT_OK

    again = run_cli("resume", thread_id, "--approve", factory=factory)

    assert again.code == EXIT_ERROR
    assert "not awaiting review" in again.err


def test_resume_unknown_thread_explains_persistence() -> None:
    result = run_cli("resume", "missing-thread", "--approve")

    assert result.code == EXIT_ERROR
    assert "no checkpoint" in result.err
    assert "SENTINEL_DATABASE_URL" in result.err


def test_invalid_address_is_an_error() -> None:
    result = run_cli("triage", "0xnot-an-address")

    assert result.code == EXIT_ERROR
    assert "invalid case" in result.err


def test_setup_errors_are_reported() -> None:
    def broken() -> AbstractAsyncContextManager[Runtime]:
        raise RuntimeError("database is down")

    result = run_cli("triage", CLEAN, factory=broken)

    assert result.code == EXIT_ERROR
    assert "database is down" in result.err
