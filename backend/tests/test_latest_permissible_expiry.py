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

**Correction (integration convergence).** The design note's own premise --
"pure calendar arithmetic ... zero F1/F2 dependency" -- does not survive
contact with F2's actual contract.
`change-pipeline/quota-raise-and-archive/CONTRACT-F2-grant.md` R11 (which this
test author, working blind, had no access to) defines the bound as
`ceiling = min(now + 7d, period_end_of(target period))`, and the shipped
`latest_permissible_expiry_for_period(now_epoch: int, period: str) -> int`
implements exactly that -- both a `now_epoch` argument and epoch-int
arithmetic throughout (matching every other `expires_at` in this codebase),
neither of which this file's original two tests exercised. Dropping the
`now + 7d` term is not a simplification available to F3: R28's whole point is
that the number the surface shows must be the number the approve endpoint
will actually accept, and near the START of a period `now + 7d` is the
BINDING term (`period_end` is three weeks away), so a display that always
renders `period_end` would show an approver a date her own approval would
then be refused for with `422 grant_window_too_short` -- the exact defect
R28 exists to prevent, reintroduced by the test's own premise. The two
original tests (both placed deliberately near month-end, where `period_end`
happens to be the binding term) never exercised the other branch; a third
test is added below for the branch they missed, strengthening this file
rather than merely fixing it to import.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _epoch(*args, **kwargs) -> int:
    return int(datetime(*args, tzinfo=timezone.utc, **kwargs).timestamp())


class TestLatestPermissibleExpiry:
    def test_is_the_last_instant_of_the_current_calendar_month_utc(self):
        from mvp.grants import latest_permissible_expiry_for_period

        # "now" is the 29th: period_end (the 30th) is only one day away, well
        # inside the 7-day window, so period_end is the binding term.
        now = _epoch(2026, 9, 29, 12, 0, 0)
        expiry = latest_permissible_expiry_for_period(now, "2026-09")
        assert expiry == _epoch(2026, 9, 30, 23, 59, 59)

    def test_is_the_last_instant_for_a_31_day_month_too(self):
        from mvp.grants import latest_permissible_expiry_for_period

        now = _epoch(2026, 8, 29, 12, 0, 0)
        expiry = latest_permissible_expiry_for_period(now, "2026-08")
        assert expiry == _epoch(2026, 8, 31, 23, 59, 59)

    def test_near_period_start_the_seven_day_window_binds_not_period_end(self):
        """F2's R11, the other half this file's original tests never
        exercised: near the START of a period, `now + 7d` falls weeks before
        `period_end`, so the 7-day window -- not the calendar month -- is
        what an approver must be shown. Showing `period_end` here would be
        exactly R28's own failure mode: a date the approve endpoint later
        refuses with `422 grant_window_too_short`."""
        from mvp.grants import latest_permissible_expiry_for_period

        now = _epoch(2026, 9, 2, 12, 0, 0)
        expiry = latest_permissible_expiry_for_period(now, "2026-09")
        assert expiry == now + 7 * 24 * 3600
        assert expiry < _epoch(2026, 9, 30, 23, 59, 59)
