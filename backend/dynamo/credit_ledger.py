"""Event-sourced credit ledger (P0-1) — the money source of truth.

The tenant-budgets counters (`pool_reserved_microusd` / `pool_settled_microusd`)
are a *materialized cache* for O(1) admission control. This ledger is the
append-only record of every money move, from which those counters are derivable
and against which they are reconciled.

Design (Fable formal design):
  - Dedicated table so append-only can be enforced at the IAM layer (PutItem +
    Query only; no Update/Delete). TransactWriteItems is cross-table, so writing
    a ledger event in the SAME transaction as the budget counter move keeps them
    atomic — a spend is recorded iff the counter moves, and vice versa.
  - The SK is the idempotency key. The terminal money move for a reservation
    (SETTLE / RELEASE / RECLAIM) is folded onto ONE sk
    `EV#HOLD#<hold_id>#TERMINAL`, so `attribute_not_exists(pk)` on insert makes
    "at most one terminal per hold" a transaction-level guarantee — a settle and
    a reaper reclaim racing the same hold cannot both land.
  - Pricing is frozen at the event: SETTLE carries the pricing_version and unit
    prices used, so an admin editing prices later never rewrites past billing.

Phase 1 (this module) ships the SETTLE event only, co-located in the existing
settle TransactWriteItems. RESERVE / RELEASE / RECLAIM / RESERVE_ADJUST are
Phase 2 (same shape, one extra Put per existing transaction).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from boto3.dynamodb.conditions import Key

from .client import credit_ledger_table_name, get_dynamodb_resource

SCHEMA_VERSION = "1"

# event_type values (Phase 1 emits SETTLE; the rest are defined for Phase 2 so
# readers/reconciliation can be written against the full set now).
EV_RESERVE = "RESERVE"
EV_SETTLE = "SETTLE"
EV_RELEASE = "RELEASE"
EV_RECLAIM = "RECLAIM"
EV_RESERVE_ADJUST = "RESERVE_ADJUST"
EV_ADJUSTMENT = "ADJUSTMENT"

# The three terminal money moves share ONE sk per hold (mutual exclusion).
_TERMINAL_TYPES = frozenset({EV_SETTLE, EV_RELEASE, EV_RECLAIM})


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def ledger_pk(tenant_id: str, period: str) -> str:
    """Partition = tenant × period (balance derivation + billing-line queries)."""
    return f"TENANT#{tenant_id}#P#{period}"


def terminal_sk(hold_id: str) -> str:
    """Single sort key for the terminal money move of a reservation.

    SETTLE / RELEASE / RECLAIM all collapse here so `attribute_not_exists`
    enforces "exactly one terminal per hold" across the settle-vs-reaper race.
    """
    return f"EV#HOLD#{hold_id}#TERMINAL"


def reserve_sk(hold_id: str) -> str:
    """Sort key for the RESERVE (credit-granted) event of a reservation."""
    return f"EV#HOLD#{hold_id}#RESERVE"


class CreditLedgerRepository:
    def __init__(self) -> None:
        self._name = credit_ledger_table_name()
        self._table = get_dynamodb_resource().Table(self._name)

    @property
    def table_name(self) -> str:
        return self._name

    # ---- transaction-item builders (composed into the caller's TransactWriteItems) ----

    def terminal_event_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        event_type: str,
        reserved_delta_microusd: int,
        settled_delta_microusd: int,
        run_id: str,
        span_id: Optional[str] = None,
        request_id: Optional[str] = None,
        group_id: Optional[str] = None,
        model_id: Optional[str] = None,
        pricing_version: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        settle_reason: Optional[str] = None,
        actor: str = "caller",
        run_id_is_fallback: bool = False,
        ts_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Build the ledger Put for a terminal money move (SETTLE/RELEASE/RECLAIM).

        `attribute_not_exists(pk)` dedupes retries AND makes the terminal
        mutually exclusive per hold — the second writer (a retried settle, or a
        reaper reclaim racing the settle) gets a ConditionalCheckFailed, which
        the caller maps to "already finalized" (an idempotent success). The GSI1
        keys (run-index) are set so a whole workflow run's money moves are
        queryable for audit.
        """
        if event_type not in _TERMINAL_TYPES:
            raise ValueError(f"terminal_event_txn_item: {event_type} is not a terminal type")
        ts = ts_ms if ts_ms is not None else _now_ms()
        event_id = terminal_sk(hold_id)[len("EV#"):]  # HOLD#<id>#TERMINAL
        item: dict[str, Any] = {
            "pk": {"S": ledger_pk(tenant_id, period)},
            "sk": {"S": terminal_sk(hold_id)},
            "event_id": {"S": event_id},
            "event_type": {"S": event_type},
            "schema_version": {"S": SCHEMA_VERSION},
            "tenant_id": {"S": tenant_id},
            "period": {"S": period},
            "hold_id": {"S": hold_id},
            "run_id": {"S": run_id},
            "reserved_delta_microusd": {"N": str(int(reserved_delta_microusd))},
            "settled_delta_microusd": {"N": str(int(settled_delta_microusd))},
            "ts_ms": {"N": str(ts)},
            "actor": {"S": actor},
            # GSI1 (run-index): per-run money-move audit trail.
            "gsi1pk": {"S": f"TENANT#{tenant_id}#RUN#{run_id}"},
            "gsi1sk": {"S": f"{ts:013d}#{event_id}"},
        }
        # Optional attribution / billing detail (omitted when None — DynamoDB
        # forbids null attribute values).
        for key, val in (
            ("span_id", span_id),
            ("request_id", request_id),
            ("group_id", group_id),
            ("model_id", model_id),
            ("pricing_version", pricing_version),
            ("settle_reason", settle_reason),
        ):
            if val:
                item[key] = {"S": str(val)}
        # Mark when run_id is a hold_id fallback (no real workflow run), so a
        # future run-level rollup can exclude these synthetic single-hold "runs"
        # rather than mistaking them for real workflow runs (Fable impl review
        # Bug 6). Immutable, so it must be recorded at write time.
        if run_id_is_fallback:
            item["run_id_source"] = {"S": "hold_id_fallback"}
        for key, num in (("tokens_in", tokens_in), ("tokens_out", tokens_out)):
            if num is not None:
                item[key] = {"N": str(int(num))}
        return {
            "Put": {
                "TableName": self._name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    # ---- read side: balance derivation + audit ----

    def sum_settled_microusd(self, *, tenant_id: str, period: str) -> int:
        """Σ settled_delta over the (tenant, period) partition — the ledger's
        derived settled total, to reconcile against `pool_settled_microusd`.

        Strongly consistent so a reconciliation double-read has a static point.
        """
        total = 0
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(ledger_pk(tenant_id, period)),
            "ConsistentRead": True,
            "ProjectionExpression": "settled_delta_microusd",
        }
        while True:
            resp = self._table.query(**kwargs)
            for it in resp.get("Items", []):
                total += int(it.get("settled_delta_microusd", 0))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    def events_for_run(self, *, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        """All money-move events for one workflow run (audit), via run-index.

        Paginated: an audit trail must not silently truncate at the 1MB page.
        """
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": "run-index",
            "KeyConditionExpression": Key("gsi1pk").eq(f"TENANT#{tenant_id}#RUN#{run_id}"),
        }
        while True:
            resp = self._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return out
            kwargs["ExclusiveStartKey"] = lek
