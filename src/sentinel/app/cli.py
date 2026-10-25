"""Command line entry point: `sentinel triage` and `sentinel resume`.

Progress and prompts go to stderr; the verdict (table or `--json`) goes to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any, TextIO

from pydantic import ValidationError

from sentinel.app.runner import (
    CaseState,
    CaseStatus,
    ProgressEvent,
    check_decision,
    new_thread_id,
    read_case,
    stream_case,
    stream_resume,
)
from sentinel.app.wiring import Runtime, RuntimeFactory, open_runtime
from sentinel.core.schemas import Action, Case, ReviewDecision, Verdict

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AWAITING_REVIEW = 3

Prompt = Callable[[str], str]

PERSISTENCE_NOTE = (
    "Resuming from another process needs a shared checkpointer: set SENTINEL_DATABASE_URL "
    "(Postgres) for both runs. Without it, checkpoints live in memory and are lost on exit."
)


def stderr_input(message: str) -> str:
    """`input()` that prompts on stderr, so stdout stays clean for `--json`."""
    print(message, end="", file=sys.stderr, flush=True)
    return input()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel", description="Multi-agent triage of suspicious crypto withdrawals."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    triage = commands.add_parser(
        "triage",
        help="triage a destination address",
        description="Run a case through the agents and print the verdict.",
        epilog=f"Exit codes: 0 verdict, 1 error, {EXIT_AWAITING_REVIEW} paused for review "
        "(--no-review). " + PERSISTENCE_NOTE,
    )
    triage.add_argument("address", help="destination address (0x...)")
    triage.add_argument("--tx-hash", help="withdrawal transaction hash")
    triage.add_argument("--amount", type=float, help="amount in asset units")
    triage.add_argument("--asset", default="ETH", help="asset symbol (default: ETH)")
    triage.add_argument("--case-id", help="case id (default: generated)")
    triage.add_argument(
        "--no-review",
        action="store_true",
        help=f"do not prompt when the case pauses; print the thread id, exit "
        f"{EXIT_AWAITING_REVIEW}",
    )
    _common(triage)

    resume = commands.add_parser(
        "resume",
        help="answer the review of a paused case",
        description="Resume a case paused for human review. " + PERSISTENCE_NOTE,
    )
    resume.add_argument("thread_id", help="thread id printed when the case paused")
    decision = resume.add_mutually_exclusive_group(required=True)
    decision.add_argument("--approve", action="store_true", help="accept the proposed action")
    decision.add_argument(
        "--override", choices=[a.value for a in Action], help="replace the proposed action"
    )
    resume.add_argument("--note", default="", help="reviewer note added to the verdict")
    _common(resume)
    return parser


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument(
        "--reviewer", default=None, help="reviewer name for decisions (default: OS user)"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_factory: RuntimeFactory | None = None,
    prompt: Prompt = stderr_input,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    cli = _Cli(args, prompt, out or sys.stdout, err or sys.stderr)
    return asyncio.run(cli.run(runtime_factory or open_runtime))


class _Cli:
    def __init__(self, args: argparse.Namespace, prompt: Prompt, out: TextIO, err: TextIO):
        self.args = args
        self.prompt = prompt
        self.out = out
        self.err = err
        self.reviewer: str = args.reviewer or getpass.getuser()

    async def run(self, factory: RuntimeFactory) -> int:
        try:
            async with factory() as runtime:
                if self.args.command == "triage":
                    return await self.triage(runtime)
                return await self.resume(runtime)
        except Exception as exc:  # report setup errors (missing keys, DB down) cleanly
            self.log(f"error: {type(exc).__name__}: {exc}")
            return EXIT_ERROR

    async def triage(self, runtime: Runtime) -> int:
        try:
            case = Case(
                case_id=self.args.case_id or f"case-{uuid.uuid4().hex[:8]}",
                address=self.args.address,
                tx_hash=self.args.tx_hash,
                amount=self.args.amount,
                asset=self.args.asset,
            )
        except ValidationError as exc:
            self.log(f"error: invalid case: {exc.errors(include_url=False)[0]['msg']}")
            return EXIT_ERROR
        thread_id = new_thread_id(case.case_id)
        self.log(f"case {case.case_id} · thread {thread_id}")
        if not await self.follow(stream_case(runtime, case, thread_id)):
            return EXIT_ERROR
        state = await read_case(runtime, thread_id)
        assert state is not None
        if state.status is CaseStatus.AWAITING_REVIEW:
            decision = None if self.args.no_review else self.ask(state)
            if decision is None:
                return self.paused(state, runtime)
            if not await self.follow(stream_resume(runtime, thread_id, decision)):
                return EXIT_ERROR
            state = await read_case(runtime, thread_id)
            assert state is not None
        return self.finish(state)

    async def resume(self, runtime: Runtime) -> int:
        state = await read_case(runtime, self.args.thread_id)
        if state is None:
            hint = "" if runtime.persistent else " " + PERSISTENCE_NOTE
            self.log(f"error: no checkpoint for thread {self.args.thread_id!r}.{hint}")
            return EXIT_ERROR
        decision = ReviewDecision(
            case_id=state.case_id,
            reviewer=self.reviewer,
            approve=self.args.approve,
            override_action=Action(self.args.override) if self.args.override else None,
            note=self.args.note,
        )
        problem = check_decision(state, decision)
        if problem is not None:
            self.log(f"error: {problem}")
            return EXIT_ERROR
        if not await self.follow(stream_resume(runtime, state.thread_id, decision)):
            return EXIT_ERROR
        final = await read_case(runtime, state.thread_id)
        assert final is not None
        return self.finish(final)

    async def follow(self, events: AsyncIterator[ProgressEvent]) -> bool:
        """Print progress; False if the run failed."""
        async for event in events:
            if event.kind == "node":
                self.log(f"  [{event.node}] {event.detail}")
            elif event.kind == "failed":
                self.log(f"error: run failed: {event.detail}")
                return False
        return True

    def ask(self, state: CaseState) -> ReviewDecision | None:
        """Interactive review; None when input ends (treated like --no-review)."""
        request = state.review_request or {}
        self.log("")
        self.log("Human review required")
        self.log(f"  proposed action: {request.get('proposed_action')}")
        self.log(f"  risk: {request.get('risk_level')} (score {request.get('score')})")
        self.log(f"  summary: {request.get('summary')}")
        for issue in request.get("critic_issues") or []:
            self.log(f"  critic issue: {issue}")
        actions = ", ".join(a.value for a in Action)
        try:
            while True:
                choice = self.prompt("Decision [a]pprove / [o]verride: ").strip().lower()
                if choice in {"a", "approve"}:
                    override = None
                    break
                if choice in {"o", "override"}:
                    raw = self.prompt(f"Action ({actions}): ").strip()
                    try:
                        override = Action(raw)
                    except ValueError:
                        self.log(f"unknown action {raw!r}")
                        continue
                    break
                self.log("please answer 'a' or 'o'")
            note = self.prompt("Note (optional): ").strip()
        except EOFError:
            return None
        return ReviewDecision(
            case_id=state.case_id,
            reviewer=self.reviewer,
            approve=override is None,
            override_action=override,
            note=note,
        )

    def paused(self, state: CaseState, runtime: Runtime) -> int:
        if self.args.json:
            self.emit_json(state)
        else:
            print(state.thread_id, file=self.out)
        self.log(
            f"paused for review. Resume with: sentinel resume {state.thread_id} "
            "--approve | --override <action>"
        )
        if not runtime.persistent:
            self.log("warning: " + PERSISTENCE_NOTE)
        return EXIT_AWAITING_REVIEW

    def finish(self, state: CaseState) -> int:
        if self.args.json:
            self.emit_json(state)
        elif state.verdict is not None:
            print(render_verdict(state.thread_id, state.verdict), file=self.out)
        if state.status is not CaseStatus.COMPLETED:
            self.log(f"error: case ended in state {state.status.value}")
            return EXIT_ERROR
        return EXIT_OK

    def emit_json(self, state: CaseState) -> None:
        print(json.dumps(state.model_dump(mode="json"), indent=2), file=self.out)

    def log(self, message: str) -> None:
        print(message, file=self.err, flush=True)


def render_verdict(thread_id: str, verdict: Verdict) -> str:
    rows: list[tuple[str, Any]] = [
        ("Case", verdict.case_id),
        ("Thread", thread_id),
        ("Action", verdict.action.value),
        ("Risk", f"{verdict.risk_level.value} (score {verdict.score})"),
        ("Reviewed by", verdict.reviewed_by or "-"),
        ("Summary", verdict.summary),
    ]
    lines = [f"{label:<12} {value}" for label, value in rows]
    if verdict.findings:
        header = ("Agent", "Signal", "Conf", "Evidence", "Rationale")
        table = [header] + [
            (
                f.agent,
                f.risk_signal.value,
                f"{f.confidence:.2f}",
                str(len(f.evidence)),
                f.rationale,
            )
            for f in verdict.findings
        ]
        widths = [max(len(row[i]) for row in table) for i in range(4)]
        lines += ["", "Findings"]
        for row in table:
            cells = [cell.ljust(width) for cell, width in zip(row[:4], widths, strict=True)]
            lines.append("  " + "  ".join([*cells, row[4]]))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
