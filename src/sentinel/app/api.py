"""HTTP API: start cases, watch their progress (SSE), and answer human reviews.

Run with `uvicorn sentinel.app.api:app`. Runs execute as background tasks in this
process; their state lives in the checkpointer, so with Postgres a case paused here
can be reviewed after a restart (or from the CLI) using only its thread id.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from sentinel.app.runner import (
    TERMINAL_KINDS,
    CaseState,
    CaseStatus,
    ProgressEvent,
    check_decision,
    new_thread_id,
    read_case,
    stream_case,
    stream_resume,
    terminal_event,
)
from sentinel.app.wiring import Runtime, RuntimeFactory, open_runtime
from sentinel.core.schemas import Case, Chain, ReviewDecision, normalize_address


class CaseRequest(BaseModel):
    """A case to triage. `case_id` is generated when omitted."""

    case_id: str | None = None
    chain: Chain = Chain.ETHEREUM
    address: str
    tx_hash: str | None = None
    asset: str = "ETH"
    amount: float | None = Field(default=None, ge=0)
    account_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return normalize_address(value)

    def to_case(self) -> Case:
        fields = self.model_dump(exclude={"case_id"})
        return Case(case_id=self.case_id or f"case-{uuid.uuid4().hex[:8]}", **fields)


class RunAccepted(BaseModel):
    thread_id: str
    case_id: str
    status: CaseStatus


class CaseView(CaseState):
    error: str | None = None


class Health(BaseModel):
    status: str = "ok"
    persistent: bool


@dataclass
class LiveRun:
    """A run executing in this process, with every event it produced so far."""

    thread_id: str
    case_id: str
    events: list[ProgressEvent] = field(default_factory=list)
    done: bool = False
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    task: asyncio.Task[None] | None = None

    @property
    def failure(self) -> str | None:
        last = self.events[-1] if self.events else None
        return last.detail if self.done and last is not None and last.kind == "failed" else None


class RunRegistry:
    """Background runs by thread id. Holds events so late SSE subscribers can replay them."""

    def __init__(self) -> None:
        self._runs: dict[str, LiveRun] = {}

    def get(self, thread_id: str) -> LiveRun | None:
        return self._runs.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        run = self._runs.get(thread_id)
        return run is not None and not run.done

    def start(self, thread_id: str, case_id: str, events: AsyncIterator[ProgressEvent]) -> LiveRun:
        run = LiveRun(thread_id=thread_id, case_id=case_id)
        self._runs[thread_id] = run
        run.task = asyncio.create_task(self._consume(run, events), name=f"triage-{thread_id}")
        return run

    @staticmethod
    async def _consume(run: LiveRun, events: AsyncIterator[ProgressEvent]) -> None:
        try:
            async for event in events:
                async with run.changed:
                    run.events.append(event)
                    run.changed.notify_all()
        finally:
            async with run.changed:
                run.done = True
                run.changed.notify_all()

    @staticmethod
    async def follow(run: LiveRun) -> AsyncIterator[ProgressEvent]:
        """Replay the run's events, then yield new ones until it ends."""
        sent = 0

        def has_news() -> bool:
            return len(run.events) > sent or run.done

        while True:
            async with run.changed:
                await run.changed.wait_for(has_news)
                pending = run.events[sent:]
                finished = run.done
            for event in pending:
                yield event
            sent += len(pending)
            if finished and sent == len(run.events):
                return

    async def aclose(self) -> None:
        tasks = [r.task for r in self._runs.values() if r.task is not None and not r.task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def _sse(event: ProgressEvent) -> str:
    return f"event: {event.kind}\ndata: {event.model_dump_json()}\n\n"


def create_app(runtime_factory: RuntimeFactory | None = None) -> FastAPI:
    """Build the API. `runtime_factory` defaults to `open_runtime()` from the environment."""
    factory: RuntimeFactory = runtime_factory or open_runtime

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory() as runtime:
            registry = RunRegistry()
            app.state.runtime = runtime
            app.state.registry = registry
            try:
                yield
            finally:
                await registry.aclose()

    app = FastAPI(title="onchain-risk-sentinel", version="0.1.0", lifespan=lifespan)

    def runtime_of(request: Request) -> Runtime:
        runtime: Runtime = request.app.state.runtime
        return runtime

    def registry_of(request: Request) -> RunRegistry:
        registry: RunRegistry = request.app.state.registry
        return registry

    async def existing_case(request: Request, thread_id: str) -> CaseState:
        state = await read_case(runtime_of(request), thread_id)
        if state is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no case with thread id {thread_id!r}")
        return state

    @app.get("/healthz")
    async def healthz(request: Request) -> Health:
        return Health(persistent=runtime_of(request).persistent)

    @app.post("/cases", status_code=status.HTTP_202_ACCEPTED)
    async def create_case(body: CaseRequest, request: Request) -> RunAccepted:
        case = body.to_case()
        thread_id = new_thread_id(case.case_id)
        registry_of(request).start(
            thread_id, case.case_id, stream_case(runtime_of(request), case, thread_id)
        )
        return RunAccepted(thread_id=thread_id, case_id=case.case_id, status=CaseStatus.RUNNING)

    @app.get("/cases/{thread_id}")
    async def get_case(thread_id: str, request: Request) -> CaseView:
        live = registry_of(request).get(thread_id)
        if live is not None and not live.done:
            return CaseView(thread_id=thread_id, case_id=live.case_id, status=CaseStatus.RUNNING)
        if live is not None and live.failure is not None:
            return CaseView(
                thread_id=thread_id,
                case_id=live.case_id,
                status=CaseStatus.FAILED,
                error=live.failure,
            )
        state = await existing_case(request, thread_id)
        return CaseView(**state.model_dump())

    @app.get("/cases/{thread_id}/events")
    async def case_events(thread_id: str, request: Request) -> StreamingResponse:
        live = registry_of(request).get(thread_id)
        if live is not None:
            events = RunRegistry.follow(live)
        else:
            state = await existing_case(request, thread_id)
            events = _single(terminal_event(state))

        async def body() -> AsyncIterator[str]:
            async for event in events:
                yield _sse(event)
                if event.kind in TERMINAL_KINDS:
                    return

        return StreamingResponse(
            body(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/cases/{thread_id}/review", status_code=status.HTTP_202_ACCEPTED)
    async def review_case(
        thread_id: str, decision: ReviewDecision, request: Request
    ) -> RunAccepted:
        registry = registry_of(request)
        if registry.is_running(thread_id):
            raise HTTPException(status.HTTP_409_CONFLICT, "case is still running")
        state = await existing_case(request, thread_id)
        if state.status is not CaseStatus.AWAITING_REVIEW:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"case is {state.status.value}, not awaiting review"
            )
        problem = check_decision(state, decision)
        if problem is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
        registry.start(
            thread_id, state.case_id, stream_resume(runtime_of(request), thread_id, decision)
        )
        return RunAccepted(thread_id=thread_id, case_id=state.case_id, status=CaseStatus.RUNNING)

    return app


async def _single(event: ProgressEvent) -> AsyncIterator[ProgressEvent]:
    yield event


app = create_app()
