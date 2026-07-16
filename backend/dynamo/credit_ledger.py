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


def _json_compact(obj: Any) -> str:
    """Deterministic compact JSON for frozen rating attributes (sorted keys, no
    spaces) — stable bytes so a replay recompute compares exactly."""
    import json

    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


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


def late_settle_sk(hold_id: str) -> str:
    """Sort key for the LATE_SETTLE (spend recovered after a RECLAIM) event.

    A DISTINCT sk namespace from `terminal_sk`, on purpose: LATE_SETTLE has
    reserved_delta ≡ 0 (it does NOT participate in the once-per-hold reserved
    return), so it lives OUTSIDE the TERMINAL mutual-exclusion cell. It is the
    settled-side correction that recovers the spend a settle would otherwise
    lose when the reaper reclaimed the hold first (Phase 2 revenue-leak fix).
    """
    return f"EV#HOLD#{hold_id}#LATE_SETTLE"


# LATE_SETTLE is a non-terminal settled-side correction; kept separate from the
# terminal types so it never enters the reserved-return exclusion.
EV_LATE_SETTLE = "LATE_SETTLE"


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
        pricing_key: Optional[str] = None,
        rating: Optional[dict] = None,
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

        Layer 5: `rating` (a RatingRecord.to_ledger_dict()) is frozen onto the
        item as a JSON string at creation time — self-contained dispute evidence
        (`recompute(rating) == settled_delta`). `pricing_version` is the frozen
        rate VERSION (not the pricing_key). Append-only: these are set once at
        creation, never updated.
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
            ("pricing_key", pricing_key),
            ("settle_reason", settle_reason),
        ):
            if val:
                item[key] = {"S": str(val)}
        if rating is not None:
            item["rating"] = {"S": _json_compact(rating)}
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

    def late_settle_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        settled_delta_microusd: int,
        run_id: str,
        run_id_is_fallback: bool = False,
        span_id: Optional[str] = None,
        request_id: Optional[str] = None,
        group_id: Optional[str] = None,
        model_id: Optional[str] = None,
        pricing_version: Optional[str] = None,
        pricing_key: Optional[str] = None,
        rating: Optional[dict] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        actor: str = "caller",
        ts_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Build the ledger Put for a LATE_SETTLE (spend recovered after RECLAIM).

        Written on a DISTINCT sk (`late_settle_sk`) with `attribute_not_exists`,
        so a late-settle retry storm still lands exactly one event. reserved_delta
        is fixed at 0: the reaper already returned `reserved` in its RECLAIM txn,
        so this event moves the settled side ONLY. Must be composed in the SAME
        TransactWriteItems as `terminal_conditioncheck_is_reclaim` so it cannot
        commit unless the terminal really is a RECLAIM (defence-in-depth against a
        mis-route; the terminal is immutable+append-only, so a read that saw
        RECLAIM cannot flip, but the ConditionCheck makes it a storage guarantee).
        """
        ts = ts_ms if ts_ms is not None else _now_ms()
        event_id = late_settle_sk(hold_id)[len("EV#"):]  # HOLD#<id>#LATE_SETTLE
        item: dict[str, Any] = {
            "pk": {"S": ledger_pk(tenant_id, period)},
            "sk": {"S": late_settle_sk(hold_id)},
            "event_id": {"S": event_id},
            "event_type": {"S": EV_LATE_SETTLE},
            "schema_version": {"S": SCHEMA_VERSION},
            "tenant_id": {"S": tenant_id},
            "period": {"S": period},
            "hold_id": {"S": hold_id},
            "run_id": {"S": run_id},
            "reserved_delta_microusd": {"N": "0"},
            "settled_delta_microusd": {"N": str(int(settled_delta_microusd))},
            "ts_ms": {"N": str(ts)},
            "actor": {"S": actor},
            "settle_reason": {"S": "late_settle"},
            "gsi1pk": {"S": f"TENANT#{tenant_id}#RUN#{run_id}"},
            "gsi1sk": {"S": f"{ts:013d}#{event_id}"},
        }
        for key, val in (
            ("span_id", span_id),
            ("request_id", request_id),
            ("group_id", group_id),
            ("model_id", model_id),
            ("pricing_version", pricing_version),
            ("pricing_key", pricing_key),
        ):
            if val:
                item[key] = {"S": str(val)}
        if rating is not None:
            item["rating"] = {"S": _json_compact(rating)}
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

    def terminal_conditioncheck_is_reclaim(
        self, *, tenant_id: str, period: str, hold_id: str
    ) -> dict[str, Any]:
        """A ConditionCheck txn item asserting the hold's terminal exists AND is a
        RECLAIM. Composed into the LATE_SETTLE transaction so the spend-recovery
        can only commit when the reaper truly reclaimed this hold — never when the
        terminal is a SETTLE (already settled) or RELEASE (client abandoned)."""
        return {
            "ConditionCheck": {
                "TableName": self._name,
                "Key": {
                    "pk": {"S": ledger_pk(tenant_id, period)},
                    "sk": {"S": terminal_sk(hold_id)},
                },
                "ConditionExpression": "attribute_exists(pk) AND event_type = :reclaim",
                "ExpressionAttributeValues": {":reclaim": {"S": EV_RECLAIM}},
            }
        }

    def reserve_event_txn_item(
        self,
        *,
        tenant_id: str,
        period: str,
        hold_id: str,
        reserved_delta_microusd: int,
        run_id: str,
        run_id_is_fallback: bool = False,
        span_id: Optional[str] = None,
        request_id: Optional[str] = None,
        group_id: Optional[str] = None,
        model_id: Optional[str] = None,
        pricing_version: Optional[str] = None,
        rate_snapshot: Optional[dict] = None,
        actor: str = "caller",
        ts_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Build the ledger Put for a RESERVE (credit granted) event, on its own
        `reserve_sk` with `attribute_not_exists` (one RESERVE per hold). Carries
        the positive reserved_delta so the reserved side (I2) is ledger-derivable:
        pool_reserved == Σ RESERVE.reserved_delta − Σ terminal reserved returned.

        Layer 5: `rate_snapshot` (a RateSnapshot.to_ledger_dict()) is frozen here
        as a JSON string, so the exact rate a reservation was admitted at is
        durable independent of the in-memory ctx. A future cross-process recovery
        can restore it via RateSnapshot.from_ledger_dict() and rate the charge
        identically (INV-R6), without depending on the live (flippable) rate
        table. Frozen at creation — append-only, never updated.
        """
        ts = ts_ms if ts_ms is not None else _now_ms()
        event_id = reserve_sk(hold_id)[len("EV#"):]  # HOLD#<id>#RESERVE
        item: dict[str, Any] = {
            "pk": {"S": ledger_pk(tenant_id, period)},
            "sk": {"S": reserve_sk(hold_id)},
            "event_id": {"S": event_id},
            "event_type": {"S": EV_RESERVE},
            "schema_version": {"S": SCHEMA_VERSION},
            "tenant_id": {"S": tenant_id},
            "period": {"S": period},
            "hold_id": {"S": hold_id},
            "run_id": {"S": run_id},
            "reserved_delta_microusd": {"N": str(int(reserved_delta_microusd))},
            "settled_delta_microusd": {"N": "0"},
            "ts_ms": {"N": str(ts)},
            "actor": {"S": actor},
            "gsi1pk": {"S": f"TENANT#{tenant_id}#RUN#{run_id}"},
            "gsi1sk": {"S": f"{ts:013d}#{event_id}"},
        }
        for key, val in (
            ("span_id", span_id),
            ("request_id", request_id),
            ("group_id", group_id),
            ("model_id", model_id),
            ("pricing_version", pricing_version),
        ):
            if val:
                item[key] = {"S": str(val)}
        if rate_snapshot is not None:
            item["rate_snapshot"] = {"S": _json_compact(rate_snapshot)}
        if run_id_is_fallback:
            item["run_id_source"] = {"S": "hold_id_fallback"}
        return {
            "Put": {
                "TableName": self._name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    # ---- read side: balance derivation + audit ----

    def get_terminal(
        self, *, tenant_id: str, period: str, hold_id: str
    ) -> Optional[dict[str, Any]]:
        """Strongly-consistent read of a hold's terminal event (or None).

        Used by the settle routing: on a terminal `attribute_not_exists` clash it
        reads WHY — SETTLE/RELEASE (idempotent / already-released) vs RECLAIM
        (recover the spend via LATE_SETTLE). ConsistentRead so the routing does
        not loop on a stale miss (safety never depends on it — the LATE_SETTLE
        txn's ConditionCheck is the final arbiter — but it makes convergence
        immediate)."""
        resp = self._table.get_item(
            Key={"pk": ledger_pk(tenant_id, period), "sk": terminal_sk(hold_id)},
            ConsistentRead=True,
        )
        return resp.get("Item")

    def get_late_settle(
        self, *, tenant_id: str, period: str, hold_id: str
    ) -> Optional[dict[str, Any]]:
        """Strongly-consistent read of a hold's LATE_SETTLE event (or None).

        Used by the late-settle recovery to compare a retry's actual against the
        already-recorded one (first-writer-wins). Goes through the key helpers so
        a pk/sk format change cannot silently drift the recovery path."""
        resp = self._table.get_item(
            Key={"pk": ledger_pk(tenant_id, period), "sk": late_settle_sk(hold_id)},
            ConsistentRead=True,
        )
        return resp.get("Item")

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

    def derived_totals(self, *, tenant_id: str, period: str) -> dict[str, Any]:
        """Fold the (tenant, period) partition into the counters the budget row
        caches, so a reconciliation batch can compare them:

            settled   = Σ settled_delta over ALL events (SETTLE + LATE_SETTLE).
                        Valid across the Phase-1→2 boundary: SETTLE terminals
                        always carried settled_delta.
            reserved  = Σ reserved_delta but ONLY over holds that HAVE a RESERVE
                        event, i.e. Phase-2-era holds. A hold whose RESERVE is
                        absent (settled/reclaimed under Phase 1, before RESERVE
                        events existed) contributes a bare terminal `-R` with no
                        matching `+R`, which would sink the derived reserved
                        spuriously negative and make every migrated tenant alarm
                        forever (Fable P2 review-2 R2-6). Excluding those holds
                        makes I2 well-defined exactly where BOTH sides of the
                        reserved lifecycle are recorded.
            reclaimed = Σ (-reserved_delta) over RECLAIM terminals whose hold also
                        has a RESERVE event (same reasoning — the Phase-1 reaper
                        wrote no RECLAIM ledger event, so pre-P2 reclaims have no
                        ledger counterpart and must not count as drift).

        `pre_p2_terminals` reports how many terminals were excluded for lacking a
        RESERVE event, so the caller can tell "reserved/reclaimed reconciliation
        is not yet meaningful for this period" (a migrating tenant) apart from a
        real zero-drift. One paginated, strongly-consistent Query. Integer
        micro-USD throughout — never a float.
        """
        settled = 0
        # Per-hold accumulation so reserved/reclaimed can be gated on "has RESERVE".
        has_reserve: set[str] = set()
        reserve_delta: dict[str, int] = {}   # hold_id -> RESERVE +R
        terminal_delta: dict[str, int] = {}  # hold_id -> terminal reserved_delta (-R)
        reclaim_returned: dict[str, int] = {}  # hold_id -> reserved returned by RECLAIM
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(ledger_pk(tenant_id, period)),
            "ConsistentRead": True,
            "ProjectionExpression": (
                "settled_delta_microusd, reserved_delta_microusd, event_type, hold_id"
            ),
        }
        while True:
            resp = self._table.query(**kwargs)
            for it in resp.get("Items", []):
                settled += int(it.get("settled_delta_microusd", 0))
                rd = int(it.get("reserved_delta_microusd", 0))
                hid = str(it.get("hold_id", ""))
                et = it.get("event_type")
                if et == EV_RESERVE:
                    has_reserve.add(hid)
                    reserve_delta[hid] = reserve_delta.get(hid, 0) + rd
                elif et in (EV_SETTLE, EV_RELEASE, EV_RECLAIM):
                    terminal_delta[hid] = terminal_delta.get(hid, 0) + rd
                    if et == EV_RECLAIM:
                        reclaim_returned[hid] = reclaim_returned.get(hid, 0) + (-rd)
                # LATE_SETTLE has reserved_delta 0; contributes to settled only.
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

        # Reserved (I2): per Phase-2-era hold, RESERVE(+R) plus its terminal(-R).
        # An open hold contributes +R (no terminal yet); a finalized one nets 0.
        # ONLY holds with a RESERVE event count — a bare pre-P2 terminal is skipped.
        reserved = sum(
            reserve_delta[hid] + terminal_delta.get(hid, 0) for hid in has_reserve
        )
        # Reclaimed (I3): reserved returned by RECLAIM, gated the same way.
        reclaimed = sum(
            amt for hid, amt in reclaim_returned.items() if hid in has_reserve
        )
        return {
            "settled_microusd": settled,
            "reserved_microusd": reserved,
            "reclaimed_microusd": reclaimed,
            # Terminals with no RESERVE event = pre-Phase-2 holds. While > 0, the
            # reserved/reclaimed axes are NOT yet fully ledger-derivable for this
            # period (the caller must not alarm on their drift).
            "pre_p2_terminals": sum(
                1 for hid in terminal_delta if hid not in has_reserve
            ),
        }

    def rating_replay_mismatches(
        self, *, tenant_id: str, period: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Layer 5 audit: TRULY replay every frozen rating in the partition and
        return the events whose rating does NOT reproduce (INV-R2/R3 violated).

        For each component this RE-COMPUTES the cost from its stored tokens × rate
        under the frozen rounding policy (`mtok_cost_for_rounding`) and checks:
          - recomputed component cost == the stored component cost_microusd, AND
          - Σ recomputed == rating.total_cost_microusd, AND
          - rating.total_cost_microusd == the event's settled_delta_microusd.

        This catches a mis-computed component that still internally sums to its
        (also-wrong) total (Fable review M2 — a plain sum could not). A healthy
        ledger returns []. Events without a `rating` attribute (pre-L5) are
        skipped. Bounded to `limit` findings (a report, not a full dump).
        """
        import json

        from mvp.pricing import mtok_cost_for_rounding

        out: list[dict[str, Any]] = []
        # hold_id -> RESERVE event's frozen snapshot version (for the INV-R6
        # cross-check: the terminal must charge at the version reserve admitted).
        reserve_version: dict[str, str] = {}
        # buffered terminal/late events with a rating, checked after the full scan
        # (so reserve_version is complete regardless of sk ordering).
        rated_events: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(ledger_pk(tenant_id, period)),
            "ConsistentRead": True,
            "ProjectionExpression": (
                "sk, hold_id, event_type, settled_delta_microusd, rating, "
                "pricing_version, rate_snapshot"
            ),
        }
        while True:
            resp = self._table.query(**kwargs)
            for it in resp.get("Items", []):
                if it.get("event_type") == EV_RESERVE:
                    snap_raw = it.get("rate_snapshot")
                    if snap_raw:
                        try:
                            reserve_version[str(it.get("hold_id"))] = str(
                                json.loads(snap_raw).get("version")
                            )
                        except (ValueError, TypeError):
                            pass
                    continue
                if it.get("rating"):
                    rated_events.append(it)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek

        for it in rated_events:
            # Hoisted out of the try so a value/parse error can never leave these
            # unbound for the mismatch test below (defensive — the except also
            # `continue`s, Fable L5-d review M3).
            margin_bad = False
            bad_component = None
            recomputed = 0
            try:
                rating = json.loads(it["rating"])
                rounding = str(rating.get("rounding", "ceil"))
                total = int(rating["total_cost_microusd"])
                settled = int(it.get("settled_delta_microusd", 0))
                for name, c in rating["components"].items():
                    want = mtok_cost_for_rounding(
                        int(c["tokens"]), int(c["rate_microusd_per_mtok"]), rounding
                    )
                    if want != int(c["cost_microusd"]):
                        bad_component = name
                    recomputed += want
                # L5-d: for a cost-bearing terminal, the frozen margin must equal
                # total - provider_cost (catches a sign/compute error at freeze).
                pc = rating.get("provider_cost_microusd")
                mg = rating.get("margin_microusd")
                if pc is not None or mg is not None:
                    if pc is None or mg is None or int(mg) != total - int(pc):
                        margin_bad = True
            except (ValueError, KeyError, TypeError):
                out.append({"hold_id": it.get("hold_id"), "sk": it.get("sk"),
                            "error": "unparseable_rating"})
                if len(out) >= limit:
                    return out
                continue
            # INV-R6 cross-check (Fable review-2 H1-residual): the charge's version
            # must equal the version the RESERVE froze — otherwise the freeze was
            # bypassed (charged at a since-flipped rate). Only when a RESERVE
            # snapshot exists for the hold (Phase-2-era, snapshot-backed).
            hid = str(it.get("hold_id"))
            version_mismatch = None
            rv = reserve_version.get(hid)
            if rv is not None and rv != rating.get("pricing_version"):
                version_mismatch = {"reserve": rv, "charged": rating.get("pricing_version")}
            if (
                bad_component is not None
                or recomputed != total
                or total != settled
                or version_mismatch is not None
                or margin_bad
            ):
                out.append({
                    "hold_id": hid,
                    "sk": it.get("sk"),
                    "event_type": it.get("event_type"),
                    "bad_component": bad_component,
                    "recomputed": recomputed,
                    "rating_total": total,
                    "settled_delta": settled,
                    "version_mismatch": version_mismatch,
                    "margin_bad": margin_bad,
                })
                if len(out) >= limit:
                    return out
        return out

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
