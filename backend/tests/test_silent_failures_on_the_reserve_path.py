"""Two failures that reported success, and one record that vanished.

Both defects here were found by reading the code, not by a failing test, and both
had the same shape: a path that cannot tell two outcomes apart reports the benign
one.

* `release_pool` read every `TransactionCanceledException` as "the hold was already
  reconciled". A cancellation says why in its reasons, and the two whys are
  opposite: a failed condition means the hold is terminal and the counter is already
  correct, while a transaction conflict means nothing was written and the
  reservation is still outstanding. Reading the second as the first left a tenant's
  headroom held until the expired-hold sweep, under a log line that said the
  reservation had been returned.

* `_build_decision_facts` unpacked the four values `price()` returns into three
  names for the untried tail of a cascade. That raised, the caller's best-effort
  fence swallowed it, and the decision record — the input a reproducible routing
  saving is computed from — was silently lost for every request whose cascade did
  not exhaust its candidates.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from mvp import _pipeline


def _cancelled(*reasons: str) -> ClientError:
    """A TransactWriteItems cancellation carrying per-item reasons, as DynamoDB
    returns them."""
    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": r} for r in reasons],
        },
        "TransactWriteItems",
    )


def _context(monkeypatch, client) -> _pipeline.ReservationContext:
    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: client)
    monkeypatch.setattr(_pipeline, "TenantBudgetsRepository", lambda: MagicMock())
    monkeypatch.setattr(_pipeline, "_reaper_ledger", lambda: MagicMock())
    monkeypatch.setattr(_pipeline.time, "sleep", lambda *_a, **_k: None)
    ctx = _pipeline.ReservationContext(
        tenants_repo=MagicMock(), reservation_tokens=4_000, period="2026-08",
    )
    ctx.tenant_id = "acme"
    ctx.pool_active = True
    ctx.pool_reserved_microusd = 5_000
    ctx.hold_id = "hold-1"
    ctx.hold_sk = "HOLD#2026-08#hold-1"
    return ctx


def test_a_condition_failure_is_the_hold_already_being_terminal(monkeypatch):
    """The reaper got there first: nothing to release, and no retry is wanted."""
    client = MagicMock()
    client.transact_write_items.side_effect = _cancelled("ConditionalCheckFailed", "None")
    ctx = _context(monkeypatch, client)
    ctx.release_pool()
    assert client.transact_write_items.call_count == 1, "a settled hold was retried"


def test_a_transaction_conflict_is_retried_because_nothing_was_written(monkeypatch):
    """The pool row was contended. The reservation is still outstanding, so giving up
    here is what leaked it until the sweep."""
    client = MagicMock()
    client.transact_write_items.side_effect = [
        _cancelled("TransactionConflict", "None"),
        None,  # the retry commits
    ]
    ctx = _context(monkeypatch, client)
    ctx.release_pool()
    assert client.transact_write_items.call_count == 2


def test_a_conflict_that_never_clears_is_reported_not_swallowed(monkeypatch, caplog):
    client = MagicMock()
    client.transact_write_items.side_effect = _cancelled("TransactionConflict", "None")
    ctx = _context(monkeypatch, client)
    ctx.release_pool()
    assert client.transact_write_items.call_count == 1 + _pipeline._RELEASE_MAX_RETRIES


def test_a_conflict_that_becomes_terminal_stops_retrying(monkeypatch):
    """The hold went terminal while we were retrying: benign, and not an error."""
    client = MagicMock()
    client.transact_write_items.side_effect = [
        _cancelled("TransactionConflict", "None"),
        _cancelled("ConditionalCheckFailed", "None"),
    ]
    ctx = _context(monkeypatch, client)
    ctx.release_pool()
    assert client.transact_write_items.call_count == 2


def test_release_is_still_called_at_most_once_per_context(monkeypatch):
    """The retry must not weaken the once-only guard: a second `release_pool()` on
    the same context is still a no-op."""
    client = MagicMock()
    ctx = _context(monkeypatch, client)
    ctx.release_pool()
    ctx.release_pool()
    assert client.transact_write_items.call_count == 1


# ---------------------------------------------------------------------------
# the decision record
# ---------------------------------------------------------------------------


def test_an_untried_cascade_tail_does_not_destroy_the_decision_record():
    """`price()` returns four values; this builder reads three. Splatting it raised,
    the fence swallowed it, and the record disappeared."""
    def price(model):
        return (f"pk-{model}", 1_234, None, None)

    facts = _pipeline._build_decision_facts(
        [("chosen", "pk-chosen", 999)],
        ["untried-a", "untried-b"],
        price,
        set(),
    )
    rejected = facts["rejected"]
    assert [r["model"] for r in rejected] == ["untried-a", "untried-b"]
    assert all(r["est_cost_microusd"] == 1_234 for r in rejected)
    assert all(r["reject_reason"] == "fallback-order" for r in rejected)


def test_each_untried_candidate_is_priced_once():
    """`price()` reads the rate table and can freeze a snapshot, so pricing a
    candidate twice would be two reads of a table that can change between them."""
    calls: list[str] = []

    def price(model):
        calls.append(model)
        return (f"pk-{model}", 10, None, None)

    _pipeline._build_decision_facts(
        [("chosen", "pk-chosen", 1)], ["a", "b", "c"], price, set()
    )
    assert calls == ["a", "b", "c"]
