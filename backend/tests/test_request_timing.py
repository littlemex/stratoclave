"""Per-phase request timing.

The instrument exists because of a specific unanswerable question: during a burst
the load balancer reported target response times up to 29.7 s while task CPU peaked
at 15%, the slowest DynamoDB write was 18.7 ms, and the same upstream called
directly at the same concurrency answered in 336 ms. Everything measurable was
fast and the request was slow, and nothing recorded which part waited.

These tests pin the two properties that make it trustworthy: it separates the
phases, and it can never break the request it measures.
"""
from __future__ import annotations

import time

import pytest

from mvp._timing import TIMING_ENV, RequestTiming, phase, timing_enabled


class TestSwitch:
    def test_on_by_default(self, monkeypatch):
        monkeypatch.delenv(TIMING_ENV, raising=False)
        assert timing_enabled()

    @pytest.mark.parametrize("off", ["false", "False", "0", "no", "off", " OFF "])
    def test_switchable_off(self, monkeypatch, off):
        """A per-request log line is a real cost at the concurrency this gateway is
        sized for, so an operator who is not diagnosing anything can turn it off."""
        monkeypatch.setenv(TIMING_ENV, off)
        assert not timing_enabled()

    @pytest.mark.parametrize("on", ["true", "1", "yes", "anything-else"])
    def test_only_explicit_negatives_disable_it(self, monkeypatch, on):
        monkeypatch.setenv(TIMING_ENV, on)
        assert timing_enabled()

    def test_disabled_emits_nothing(self, monkeypatch):
        monkeypatch.setenv(TIMING_ENV, "false")
        lines = []
        monkeypatch.setattr(
            "mvp._timing.logger", type("L", (), {"info": lambda self, *a, **k: lines.append(k)})()
        )
        t = RequestTiming()
        with phase(t, "reserve"):
            pass
        t.emit(route="x")
        assert lines == []


class TestPhases:
    def _capture(self, monkeypatch) -> list[dict]:
        lines: list[dict] = []
        monkeypatch.setattr(
            "mvp._timing.logger",
            type("L", (), {"info": lambda self, event, **k: lines.append({"event": event, **k})})(),
        )
        return lines

    def test_each_phase_is_reported_separately(self, monkeypatch):
        """The whole point: 'waiting on the model' and 'waiting on ourselves' have
        to be distinguishable, or the next guess is a guess."""
        monkeypatch.setenv(TIMING_ENV, "true")
        lines = self._capture(monkeypatch)

        t = RequestTiming()
        with phase(t, "reserve"):
            time.sleep(0.01)
        with phase(t, "upstream"):
            time.sleep(0.03)
        with phase(t, "settle"):
            time.sleep(0.01)
        t.emit(route="chat_completions", outcome="ok")

        assert len(lines) == 1
        line = lines[0]
        assert line["event"] == "request_timing"
        assert line["upstream_ms"] > line["reserve_ms"]
        assert line["total_ms"] >= line["upstream_ms"]
        assert line["route"] == "chat_completions"

    def test_time_outside_the_named_phases_is_visible(self, monkeypatch):
        """A large remainder is the signal that the wait is somewhere nobody is
        looking yet, which is exactly the situation this was written for."""
        monkeypatch.setenv(TIMING_ENV, "true")
        lines = self._capture(monkeypatch)

        t = RequestTiming()
        with phase(t, "upstream"):
            time.sleep(0.005)
        time.sleep(0.03)  # unaccounted
        t.emit(route="chat_completions")

        assert lines[0]["unaccounted_ms"] > lines[0]["upstream_ms"]

    def test_a_repeated_phase_accumulates(self, monkeypatch):
        """A Converse failover calls upstream twice, and a partial stream settles
        twice; the total is what matters."""
        monkeypatch.setenv(TIMING_ENV, "true")
        lines = self._capture(monkeypatch)

        t = RequestTiming()
        for _ in range(3):
            with phase(t, "upstream"):
                time.sleep(0.01)
        t.emit(route="chat_completions")
        assert lines[0]["upstream_ms"] >= 25

    def test_an_unstarted_phase_is_ignored(self):
        t = RequestTiming()
        t.stop("never_started")  # must not raise


class TestItCannotBreakTheRequest:
    def test_a_broken_logger_is_swallowed(self, monkeypatch):
        """An instrument that breaks the thing it measures is worse than none. The
        emit sits in a `finally` on the money path."""
        monkeypatch.setenv(TIMING_ENV, "true")

        class Boom:
            def info(self, *a, **k):
                raise RuntimeError("log sink is down")

        monkeypatch.setattr("mvp._timing.logger", Boom())
        t = RequestTiming()
        with phase(t, "reserve"):
            pass
        t.emit(route="chat_completions")  # must not raise

    def test_phase_tolerates_no_stopwatch(self):
        """Call sites stay readable by not branching on whether timing exists."""
        with phase(None, "reserve"):
            pass
