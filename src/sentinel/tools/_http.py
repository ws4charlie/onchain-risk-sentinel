"""Small HTTP helpers shared by adapters: a request-rate limiter and retry with backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# httpx logs full request URLs at INFO, and provider URLs carry API keys.
logging.getLogger("httpx").setLevel(logging.WARNING)


class RateLimiter:
    """Allow at most `rate` calls per second by spacing calls evenly. Safe across tasks."""

    def __init__(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self._interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self._interval
        if delay > 0:
            await asyncio.sleep(delay)


class RetryableError(Exception):
    """Raised by a request callback to ask for another attempt (e.g. an in-body rate limit)."""


def backoff_delay(attempt: int, base: float, retry_after: str | None = None) -> float:
    """Exponential backoff (base * 2**attempt), or the server's Retry-After seconds if given."""
    if retry_after is not None:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    return base * 2.0**attempt


async def request_with_retry(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    max_retries: int,
    backoff_base: float,
    check: Callable[[httpx.Response], None] | None = None,
) -> httpx.Response:
    """Call `send` until it returns a non-retryable response.

    Retries on transport errors, 429/5xx, and `RetryableError` raised by `check`.
    Re-raises the last error once `max_retries` extra attempts are used up.
    """
    attempt = 0
    while True:
        retry_after: str | None = None
        try:
            response = await send()
            retry_after = response.headers.get("Retry-After")
            response.raise_for_status()
            if check is not None:
                check(response)
            return response
        except (httpx.TransportError, httpx.HTTPStatusError, RetryableError) as exc:
            retryable = not isinstance(exc, httpx.HTTPStatusError) or (
                exc.response.status_code in RETRYABLE_STATUS
            )
            if not retryable or attempt >= max_retries:
                raise
        await asyncio.sleep(backoff_delay(attempt, backoff_base, retry_after))
        attempt += 1
