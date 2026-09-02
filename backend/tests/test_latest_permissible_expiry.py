"""F3 / R28 (display half only) — the latest permissible expiry shown before
it is typed.

Contract: `change-pipeline/quota-raise-and-archive/CONTRACT-F3-surfaces.md`, id R28.

Seam amendment B1 (the integration owner's seam notes, §S10, outside this repository)
narrows this id's F3 half: "R28's
suspended-pool refusal is a server-side lifecycle rule and moves to F2; F3
keeps only the display of the latest permissible expiry." The same reasoning
applies to enforcing `expires_at <= period_end` — that is the approve
endpoint's own validation, F2's now, not F3's to build or test.

This file used to also contain `TestApprovalRefusedOnSuspendedPool` (asserting
the approve endpoint 402s on a suspended pool) and a test asserting the
approve endpoint 422s on an out-of-bounds expiry. Both are DELETED per B1 —
they tested F2's server-side lifecycle enforcement, not an F3 surface. What
remains is the ONE fact F3 still owns: computing the value to display, with
zero F1/F2 dependency — pure calendar arithmetic on the real, already-merged
`current_period()` (`backend/dynamo/tenant_budgets.py:254`, calendar month
UTC).

This role's design note places the function in `backend/mvp/grants.py` (F2's
file — the approve flow that enforces the bound lives there, and the
approval view's GET already reads the composition from F2 per R30/R21b's
corrections), flagged as a placement guess. The test therefore still fails at
import today, for the same "surface absent" reason as before — only the
module it imports from changed.
"""
from __future__ import annotations

from datetime import datetime, timezone


class TestLatestPermissibleExpiry:
    def test_is_the_last_instant_of_the_current_calendar_month_utc(self):
        from mvp.grants import latest_permissible_expiry_for_period

        # 2026-09 -> the last instant of September 2026, UTC. This is pure
        # calendar math on the SAME period string current_period() returns —
        # no F1/F2 fact required, only a function that does not exist yet.
        expiry = latest_permissible_expiry_for_period("2026-09")
        assert expiry == datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)

    def test_is_the_last_instant_for_a_31_day_month_too(self):
        from mvp.grants import latest_permissible_expiry_for_period

        expiry = latest_permissible_expiry_for_period("2026-08")
        assert expiry == datetime(2026, 8, 31, 23, 59, 59, tzinfo=timezone.utc)
