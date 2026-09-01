"""F3 / R38 (usage-view half) — a fallback caused by a grant expiry is visible
as such in the usage view, distinct from an ordinary quota/order fallback.

Contract: `change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`, id R38.

  "A user cannot distinguish 'my grant expired' from 'the router changed its
  mind', and a usage view spanning an expiry shows a model change with no
  cause. Unit: the refusal and the usage view both name the expiry within the
  defined window."

This targets REAL, already-merged code — no F1/F2 dependency for this half.
`backend/dynamo/usage_logs.py::UsageLogsRepository.record()` (already in this
worktree) persists `model_id` / `requested_model_id` only; `fallback_occurred`
is derived at READ time purely by comparing the two ids
(`backend/mvp/me.py::_derive_fallback`) — deliberately, per that function's
own docstring, with "no second source of truth" for WHY the fallback
happened. R38 needs a cause, which cannot be reconstructed after the fact
from the two model ids alone: it must be captured at write time.

This deliverable's design note proposes `record(..., fallback_reason=...)`.
`record()` does not accept that keyword today — this is not a missing module
(unlike R24/R25/R28's F1/F2-owned surfaces), it is a real, precise gap in a
file already in F3's reach: `backend/dynamo/usage_logs.py` is now confirmed
in scope for this one additive attribute (contract correction), so this
`TypeError` is the failure to keep, not a scope question.

R38's naming window is `expires_at <= now <= expires_at + 15 minutes` (three
5-minute sweep intervals, so a late sweep still gets to name the cause), and
the wall that refused must be the SAME wall the grant raised (a grant against
`tenant_pool` cannot explain a `per_model_user` refusal). `TestGrantExpiryWindow`
below pins both halves of that rule against a hypothesized
`mvp._pipeline.fallback_reason_for_expired_grant` — it does not exist yet, so
these fail at `AttributeError`, the same "surface absent" reason as the rest
of this file's first test.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dynamo.usage_logs import UsageLogsRepository


class TestFallbackCauseCapture:
    def test_record_accepts_and_persists_a_fallback_reason(self, dynamodb_mock):
        """The behaviour R38 requires: `record()` must accept a cause and
        round-trip it, so the read side can later tell "grant expired" apart
        from an ordinary fallback. Today `record()` has no `fallback_reason`
        parameter at all — this call raises `TypeError` (unexpected keyword
        argument), which IS the failure: the id names a fact the write path
        cannot yet capture.
        """
        repo = UsageLogsRepository()
        item = repo.record(  # type: ignore[call-arg]
            tenant_id="acme-eng",
            user_id="user-1",
            user_email="user@acme.example",
            model_id="claude-haiku-4-5",
            input_tokens=10,
            output_tokens=5,
            requested_model_id="claude-opus-4-7",
            fallback_reason="grant_expired",
        )
        assert item["fallback_reason"] == "grant_expired"

    def test_a_fallback_row_is_indistinguishable_from_an_ordinary_one_today(
        self, dynamodb_mock
    ):
        """Positive control: record two fallback rows for different causes
        (one because a grant expired, one an ordinary quota-order fallback)
        the only way `record()` currently allows — with no cause at all — and
        show the stored items are byte-identical apart from ids/timestamps.
        This is the exact defect R38 names: "a user cannot distinguish 'my
        grant expired' from 'the router changed its mind'" — today, neither
        can the stored row.
        """
        repo = UsageLogsRepository()
        item_a = repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            requested_model_id="claude-opus-4-7",
        )
        item_b = repo.record(
            tenant_id="acme-eng", user_id="user-1", user_email="user@acme.example",
            model_id="claude-haiku-4-5", input_tokens=10, output_tokens=5,
            requested_model_id="claude-opus-4-7",
        )
        fields_that_could_carry_a_cause = set(item_a) - {"timestamp_log_id", "recorded_at"}
        for field in fields_that_could_carry_a_cause:
            assert item_a[field] == item_b[field], (
                f"field {field!r} differs between the two fallback rows, but "
                "no field currently distinguishes WHY either fallback "
                "happened — this assertion is expected to hold today "
                "(pinning the absence), and the id requires it to stop "
                "holding once fallback_reason exists."
            )
        assert "fallback_reason" not in item_a


class TestGrantExpiryWindow:
    """R38's naming window, pinned per the contract correction:
    `expires_at <= now <= expires_at + 15 minutes`, and the wall that refused
    must be the SAME wall the grant raised. Neither half is negotiable per
    the corrected contract, so both are pinned exactly rather than left as a
    named-but-unsized parameter.

    Targets a hypothesized `mvp._pipeline.fallback_reason_for_expired_grant`
    (design note, R38) — `mvp._pipeline` itself exists and imports cleanly,
    so these fail at `AttributeError` on the missing function, not at
    `ModuleNotFoundError` — the precise "this classification does not exist
    yet" reason, on top of real, already-merged code.
    """

    def test_within_the_15_minute_window_and_matching_wall_names_the_cause(self):
        from mvp import _pipeline

        expires_at = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        now = expires_at + timedelta(minutes=10)  # inside the 15-minute window
        assert _pipeline.fallback_reason_for_expired_grant(
            grant_expires_at=expires_at,
            now=now,
            grant_wall="tenant_pool",
            blocked_wall="tenant_pool",
        ) == "grant_expired"

    def test_exactly_15_minutes_still_names_the_cause_inclusive_bound(self):
        from mvp import _pipeline

        expires_at = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        now = expires_at + timedelta(minutes=15)  # the boundary itself
        assert _pipeline.fallback_reason_for_expired_grant(
            grant_expires_at=expires_at,
            now=now,
            grant_wall="tenant_pool",
            blocked_wall="tenant_pool",
        ) == "grant_expired"

    def test_past_the_15_minute_window_does_not_name_the_cause(self):
        from mvp import _pipeline

        expires_at = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        now = expires_at + timedelta(minutes=16)  # one minute past three sweeps
        assert _pipeline.fallback_reason_for_expired_grant(
            grant_expires_at=expires_at,
            now=now,
            grant_wall="tenant_pool",
            blocked_wall="tenant_pool",
        ) is None

    def test_mismatched_wall_does_not_name_the_cause_even_inside_the_window(self):
        from mvp import _pipeline

        # The grant that expired covered `tenant_pool`; the wall that
        # actually refused was a per-model USER quota — a different wall, so
        # the expired grant is not the proximate cause even though it is
        # within the window.
        expires_at = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
        now = expires_at + timedelta(minutes=5)
        assert _pipeline.fallback_reason_for_expired_grant(
            grant_expires_at=expires_at,
            now=now,
            grant_wall="tenant_pool",
            blocked_wall="per_model_user",
        ) is None
