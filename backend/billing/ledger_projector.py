"""DynamoDB Streams projector: derive RESERVE ledger events from HOLD records.

Two-item migration step 1 (docs/design/ledger-hot-path.md). Today the RESERVE
ledger event is written SYNCHRONOUSLY inside the reserve TransactWriteItems — one
of the four items whose count drives the single-pool-row contention tail. This
projector re-derives that same event ASYNCHRONOUSLY from the HOLD row's stream
record, so the synchronous item can eventually be dropped.

Design guarantees (Fable review):

  * DERIVE FROM THE HOLD ITEM ONLY. HOLD rows are per-hold partition-ordered on
    the stream, so a hold's terminal record can never precede its INSERT. (Pool
    rows are NOT a source here — their ordering is independent of the hold.)
  * DETERMINISTIC content. No now()/random in the projector — the event is a pure
    function of the HOLD image, so an at-least-once redelivery re-computes the
    SAME bytes and the idempotent conditional Put is a true no-op.
  * IDEMPOTENT write. `attribute_not_exists(pk)` on `(pk, reserve_sk)`; a second
    delivery of the same INSERT CCFs and is swallowed.
  * SHADOW mode. When `SHADOW#`-prefixed (migration step 1), the projector writes
    to a shadow sk namespace so a reconciler can diff it against the still-
    synchronous real RESERVE event and prove divergence == 0 before cutover.

This module is import-safe in the app image (the Lambda handler shares the repo
code) and has NO import-time AWS calls; `handler` builds its client lazily.
"""
from __future__ import annotations

from typing import Any, Optional

from dynamo.credit_ledger import (
    EV_RESERVE,
    SCHEMA_VERSION,
    ledger_pk,
    reserve_sk,
)

SHADOW_PREFIX = "SHADOW#"


def _s(image: dict, key: str) -> Optional[str]:
    v = image.get(key)
    if isinstance(v, dict):  # low-level stream image: {"S": "..."} / {"N": "..."}
        return v.get("S") or v.get("N")
    return None if v is None else str(v)


def _n(image: dict, key: str) -> Optional[int]:
    raw = _s(image, key)
    return int(raw) if raw is not None and raw != "" else None


def is_hold_record(new_image: dict) -> bool:
    """True iff this NEW image is a HOLD row (sk begins with 'HOLD#'). The
    projector ignores BUDGET rows and anything else on the budgets stream."""
    sk = _s(new_image, "sk")
    return bool(sk and sk.startswith("HOLD#"))


def reserve_event_from_hold(new_image: dict, *, shadow: bool = False) -> Optional[dict[str, Any]]:
    """Pure derivation: an enriched HOLD INSERT image → the RESERVE ledger event
    Put item (low-level attribute-value form), or None if the image lacks the
    enrichment needed to derive it (a pre-migration HOLD — skip, not fail).

    Byte-goal: the derived event matches what
    CreditLedgerRepository.reserve_event_txn_item wrote synchronously, for the
    fields a reconciler compares (event_type, hold_id, reserved_delta, source,
    run_id, rate_snapshot). ts_ms is taken from the HOLD's created_at-derived
    field when present, else omitted — the reconciler does not compare ts.
    """
    if not is_hold_record(new_image):
        return None
    hold_id = _s(new_image, "hold_id")
    tenant_id = _s(new_image, "tenant_id")
    period = _s(new_image, "period")
    amount = _n(new_image, "amount_microusd")
    if not (hold_id and tenant_id and period and amount is not None):
        return None
    # Only external + inline enriched holds carry `source`; a pre-enrichment HOLD
    # has none → nothing to project (the synchronous RESERVE event still exists).
    source = _s(new_image, "source")
    if source is None:
        return None

    sk = reserve_sk(hold_id)
    if shadow:
        sk = SHADOW_PREFIX + sk
    event_id = reserve_sk(hold_id)[len("EV#"):]  # HOLD#<id>#RESERVE (no shadow)
    run_id = _s(new_image, "run_id") or hold_id
    item: dict[str, Any] = {
        "pk": {"S": ledger_pk(tenant_id, period)},
        "sk": {"S": sk},
        "event_id": {"S": event_id},
        "event_type": {"S": EV_RESERVE},
        "schema_version": {"S": SCHEMA_VERSION},
        "tenant_id": {"S": tenant_id},
        "period": {"S": period},
        "hold_id": {"S": hold_id},
        "run_id": {"S": run_id},
        "reserved_delta_microusd": {"N": str(int(amount))},
        "settled_delta_microusd": {"N": "0"},
        "actor": {"S": "projector"},
        "derived": {"S": "streams"},
    }
    desc = _s(new_image, "hold_description")
    if desc:
        item["description"] = {"S": desc}
    if source:
        item["source"] = {"S": source}
    rate = _s(new_image, "rate_snapshot")
    if rate:
        item["rate_snapshot"] = {"S": rate}
    if _s(new_image, "run_id_source") == "hold_id_fallback":
        item["run_id_source"] = {"S": "hold_id_fallback"}
    return item


def _new_image(record: dict) -> Optional[dict]:
    """Extract the NEW image from a DynamoDB stream record, INSERT only.
    (MODIFY/REMOVE are terminal-side transitions handled synchronously today.)"""
    if record.get("eventName") != "INSERT":
        return None
    return record.get("dynamodb", {}).get("NewImage")


def handler(event, context=None):  # noqa: ARG001 — Lambda signature
    """Stream event-source handler. Derives a RESERVE event per new HOLD record
    and writes it idempotently. Returns `batchItemFailures` (partial-batch
    response) so a transient write failure retries ONLY that record and never
    silently drops an audit event.

    `LEDGER_PROJECTOR_SHADOW` (env, default "true") writes to the SHADOW# sk
    namespace during migration step 1.
    """
    import os

    import boto3
    from botocore.exceptions import ClientError

    from dynamo.client import credit_ledger_table_name

    shadow = os.getenv("LEDGER_PROJECTOR_SHADOW", "true").lower() == "true"
    client = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    table = credit_ledger_table_name()

    failures: list[dict] = []
    for record in event.get("Records", []):
        seq = record.get("dynamodb", {}).get("SequenceNumber")
        try:
            img = _new_image(record)
            if img is None:
                continue
            item = reserve_event_from_hold(img, shadow=shadow)
            if item is None:
                continue
            try:
                client.put_item(
                    TableName=table,
                    Item=item,
                    ConditionExpression="attribute_not_exists(pk)",
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    # Already projected — an at-least-once redelivery of the SAME
                    # INSERT is a true no-op. This is only safe because a HOLD is
                    # written exactly once (fresh-uuid hold_id, unique sk, HOLD Put
                    # is attribute_not_exists) so a second INSERT for the same
                    # hold_id — hence the same reserve_sk — cannot legitimately
                    # occur. If a future design re-INSERTs a HOLD under the same
                    # hold_id with a DIFFERENT amount (e.g. expiry extension via
                    # new sk mapping to the same reserve_sk), this swallow would
                    # hide the change; the reconciler's field_diff is the backstop
                    # (it compares reserved_delta), and that design must add a
                    # read-back-and-compare here before it ships (Fable review 3).
                    continue
                raise
        except Exception:  # noqa: BLE001 — surface to partial-batch retry, never drop.
            if seq:
                failures.append({"itemIdentifier": seq})
    return {"batchItemFailures": failures}


# ---------------------------------------------------------------------------
# Reconciler (migration step 1 gate): shadow event vs synchronous RESERVE event.
# ---------------------------------------------------------------------------

# The fields a reconciler compares between the shadow (Streams-derived) event and
# the synchronous RESERVE event. ts_ms / actor / derived deliberately differ (the
# projector stamps its own), so they are NOT compared.
_RECONCILE_FIELDS = (
    "event_type", "hold_id", "reserved_delta_microusd", "settled_delta_microusd",
    "source", "run_id", "run_id_source", "description", "rate_snapshot",
)


def diff_events(shadow_item: dict, real_item: dict) -> dict[str, tuple]:
    """Return {field: (shadow_value, real_value)} for every reconciled field that
    differs. Empty dict == the shadow projection matches the synchronous event.
    Both items are resource-API rows (plain Python values)."""
    out: dict[str, tuple] = {}
    for f in _RECONCILE_FIELDS:
        sv, rv = shadow_item.get(f), real_item.get(f)
        # normalize Decimal/int for the numeric fields
        if f.endswith("_microusd"):
            sv = int(sv) if sv is not None else None
            rv = int(rv) if rv is not None else None
        if sv != rv:
            out[f] = (sv, rv)
    return out


def reconcile_partition(table, tenant_id: str, period: str, *,
                        now_ms: Optional[int] = None,
                        lag_budget_ms: int = 15 * 60 * 1000) -> dict[str, Any]:
    """Scan one tenant×period ledger partition and compare each SHADOW# RESERVE
    projection to its synchronous RESERVE event. Returns a summary with the
    divergence count and any mismatching hold_ids. Read-only — never writes.

    Divergence classes:
      * `missing_real`  — shadow exists, synchronous RESERVE absent (projector ran
        for a hold the app never wrote synchronously — should be impossible while
        dual-write is on; flags a bug).
      * `missing_shadow`— synchronous RESERVE exists, no shadow. This is only
        BENIGN LAG within the stream-processing budget; a synchronous RESERVE
        OLDER than `lag_budget_ms` with no shadow is a projector that PERMANENTLY
        dropped the event (a source-detection or image-parse bug) — counted as
        divergence, NOT hidden as lag (Fable review finding 4).
      * `field_diff`    — both exist but a reconciled field differs.

    `now_ms` / `lag_budget_ms` are injectable for tests; `now_ms` defaults to the
    wall clock (the reconciler runs in a live Lambda where that is allowed).
    """
    from boto3.dynamodb.conditions import Key

    if now_ms is None:
        import time
        now_ms = int(time.time() * 1000)

    pk = ledger_pk(tenant_id, period)
    items = []
    kwargs = {"KeyConditionExpression": Key("pk").eq(pk)}
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek

    reserve_by_hold: dict[str, dict] = {}
    shadow_by_hold: dict[str, dict] = {}
    for it in items:
        sk = str(it.get("sk", ""))
        if sk.startswith(SHADOW_PREFIX + "EV#HOLD#") and sk.endswith("#RESERVE"):
            shadow_by_hold[str(it.get("hold_id"))] = it
        elif sk.startswith("EV#HOLD#") and sk.endswith("#RESERVE"):
            reserve_by_hold[str(it.get("hold_id"))] = it

    missing_real, field_diff = [], {}
    lagging_shadow, stale_missing_shadow = [], []
    for hid, shadow in shadow_by_hold.items():
        real = reserve_by_hold.get(hid)
        if real is None:
            missing_real.append(hid)
            continue
        d = diff_events(shadow, real)
        if d:
            field_diff[hid] = d
    for hid, real in reserve_by_hold.items():
        if hid in shadow_by_hold:
            continue
        # Classify by the synchronous RESERVE's age: young = the projector just
        # hasn't caught up (lag); old = it never will (a dropped event = bug).
        try:
            real_ts = int(real.get("ts_ms", 0))
        except (TypeError, ValueError):
            real_ts = 0
        if real_ts and (now_ms - real_ts) > lag_budget_ms:
            stale_missing_shadow.append(hid)
        else:
            lagging_shadow.append(hid)

    divergence = len(missing_real) + len(field_diff) + len(stale_missing_shadow)
    return {
        "tenant_id": tenant_id, "period": period,
        "shadow_count": len(shadow_by_hold), "real_count": len(reserve_by_hold),
        "divergence": divergence,
        "missing_real": missing_real,
        "missing_shadow": stale_missing_shadow,   # only the BUG class (backward-compatible key)
        "lagging_shadow": lagging_shadow,          # benign in-budget lag
        "field_diff": field_diff,
    }
