"""Where a request's wall-clock time went.

Added for a specific question that could not be answered from the outside. During a
burst on 2026-08-25 the load balancer reported target response times up to 29.7 s
while task CPU peaked at 15%, DynamoDB's slowest write was 18.7 ms and the same
upstream called directly from the same host answered in 336 ms at the same
concurrency. Everything measurable was fast and the request was slow, which means
the time was spent waiting somewhere inside the process — and nothing recorded
where.

So each phase is timed separately: the reservation, the upstream call, the
settlement, and the total. A single line per request with those four numbers
separates "waiting on the model" from "waiting on ourselves", which is the
distinction every further guess depends on.

Deliberately cheap and deliberately switchable. `time.perf_counter` costs
nanoseconds, the line is emitted once per request, and `GATEWAY_REQUEST_TIMING=false`
turns it off without a code change — a per-request log line is a real cost at the
concurrency this gateway is now sized for, and an operator who is not diagnosing
anything should not have to pay it.
"""
from __future__ import annotations

import os
import time
from typing import Any, Optional

from core.logging import get_logger

logger = get_logger(__name__)

TIMING_ENV = "GATEWAY_REQUEST_TIMING"


def timing_enabled() -> bool:
    """On unless switched off. Read per call so a task can be flipped by redeploy
    without a code change, and so a test can toggle it."""
    return (os.getenv(TIMING_ENV, "true") or "true").strip().lower() not in (
        "false",
        "0",
        "no",
        "off",
    )


class RequestTiming:
    """Phase stopwatch for one request.

    Every method is safe to call whether timing is on or off, and none of them can
    raise into the request: an instrument that breaks the thing it measures is
    worse than no instrument.
    """

    __slots__ = ("_started", "_phases", "_open")

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._phases: dict[str, float] = {}
        self._open: dict[str, float] = {}

    def start(self, phase: str) -> None:
        self._open[phase] = time.perf_counter()

    def stop(self, phase: str) -> None:
        began = self._open.pop(phase, None)
        if began is None:
            return
        # Accumulate: a phase can happen more than once (a Converse failover, a
        # settle after a partial stream), and the total is what matters.
        self._phases[phase] = self._phases.get(phase, 0.0) + (
            time.perf_counter() - began
        )

    def emit(self, **context: Any) -> None:
        """Log the line. Called once, from a `finally`, so a failed request is
        measured too — those are the interesting ones."""
        if not timing_enabled():
            return
        try:
            total_ms = (time.perf_counter() - self._started) * 1000
            phases = {f"{name}_ms": round(ms * 1000, 1) for name, ms in self._phases.items()}
            # What is left after the phases we named. A large remainder is the
            # signal that the wait is somewhere we are not yet looking.
            unaccounted = total_ms - sum(ms * 1000 for ms in self._phases.values())
            logger.info(
                "request_timing",
                total_ms=round(total_ms, 1),
                unaccounted_ms=round(unaccounted, 1),
                **phases,
                **context,
            )
        except Exception:  # noqa: BLE001 — never break a request to log about it
            pass


def phase(timing: Optional[RequestTiming], name: str):
    """Context manager around one phase, tolerant of `None`.

    Call sites stay readable and do not have to branch on whether timing exists.
    """

    class _Phase:
        def __enter__(self):
            if timing is not None:
                timing.start(name)
            return self

        def __exit__(self, *exc):
            if timing is not None:
                timing.stop(name)
            return False

    return _Phase()
