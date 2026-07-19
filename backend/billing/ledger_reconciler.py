"""Scheduled reconciler: shadow projection vs synchronous RESERVE event.

Two-item migration step 1 gate (docs/design/ledger-hot-path.md). Runs on an
EventBridge schedule, scans the credit-ledger for SHADOW# RESERVE projections and
diffs them against the synchronous RESERVE events via
`ledger_projector.reconcile_partition`. Emits a single structured summary line
(and a CloudWatch metric via EMF) so the migration can be gated on
`divergence == 0` before the HOLD-only cutover and the async cut-over.

Read-only: it never writes to the ledger. A non-zero divergence is an alarm
signal, not a repair action — repair (re-project from the HOLD rows) is a
deliberate, separately-triggered step.
"""
from __future__ import annotations

import json
import os
from typing import Any


def _iter_ledger_partitions(table):
    """Yield distinct (tenant_id, period) of every RESERVE/SHADOW RESERVE row.

    A full Scan is acceptable for a scheduled reconciler over the migration
    window; it is bounded by the projected/synchronous RESERVE rows, not the hot
    path. (When the ledger grows large this becomes a GSI-backed query, but for
    the migration gate a scan with a projection expression is sufficient.)
    """
    seen: set[tuple[str, str]] = set()
    kwargs: dict[str, Any] = {
        "ProjectionExpression": "tenant_id, #p, sk",
        "ExpressionAttributeNames": {"#p": "period"},
    }
    while True:
        resp = table.scan(**kwargs)
        for it in resp.get("Items", []):
            sk = str(it.get("sk", ""))
            if "EV#HOLD#" in sk and sk.endswith("#RESERVE"):
                key = (str(it.get("tenant_id")), str(it.get("period")))
                if all(key):
                    seen.add(key)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            break
        kwargs["ExclusiveStartKey"] = lek
    return sorted(seen)


def handler(event=None, context=None):  # noqa: ARG001 — Lambda signature
    import time

    import boto3

    from billing.ledger_projector import (
        count_post_epoch_sourceless_holds,
        reconcile_partition,
    )
    from dynamo.client import credit_ledger_table_name, tenant_budgets_table_name

    ddb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    table = ddb.Table(credit_ledger_table_name())

    # The projector starts at LATEST, so RESERVE events minted BEFORE it went live
    # have no shadow by construction — that historical backlog is NOT divergence.
    # PROJECTOR_EPOCH_MS bounds the gate to the projector's actual domain; set it
    # to the projector's deploy time (epoch ms). Unset (0) = compare everything
    # (correct only for a table with no pre-projector RESERVE events).
    epoch_ms = int(os.getenv("PROJECTOR_EPOCH_MS", "0"))

    total_divergence = 0
    partitions = 0
    worst: list[dict] = []
    for tenant_id, period in _iter_ledger_partitions(table):
        partitions += 1
        summ = reconcile_partition(table, tenant_id, period, projector_epoch_ms=epoch_ms)
        total_divergence += summ["divergence"]
        if summ["divergence"] or summ["missing_shadow"]:
            worst.append(summ)

    # Step-3 gate (Fable review-2 finding 2): detect an enrichment epoch set too
    # early. A HOLD minted at/after ENRICHMENT_EPOCH_MS but with NO `source` would
    # be routed to the capture/void HOLD-only path and 404 an authorized txn. This
    # count MUST be zero; it is also the safety gate for deleting the RESERVE-event
    # fallback. Independent of the projector epoch (different concern).
    enrichment_epoch_ms = int(os.getenv("ENRICHMENT_EPOCH_MS", "0"))
    budgets_table = ddb.Table(tenant_budgets_table_name())
    epoch_check = count_post_epoch_sourceless_holds(budgets_table, enrichment_epoch_ms)
    post_epoch_sourceless = epoch_check["post_epoch_sourceless"]

    # EMF requires a Timestamp; a scheduled EventBridge event carries none, so an
    # earlier "pop if absent" version silently dropped the metric — leaving the
    # divergence ALARM with no data and a cut-over ungated (Fable review finding 4,
    # CONFIRMED). ALWAYS stamp `now` so the gate metric is emitted every run.
    ts_ms = int(time.time() * 1000)
    result = {
        "reconciler": "ledger_reserve_shadow",
        "partitions": partitions,
        "total_divergence": total_divergence,
        "post_epoch_sourceless_holds": post_epoch_sourceless,
        "enrichment_epoch_ms": enrichment_epoch_ms,
        # EMF-style embedded metrics so CloudWatch alarms can gate on both the
        # shadow divergence and the enrichment-epoch misconfiguration.
        "_aws": {
            "CloudWatchMetrics": [{
                "Namespace": "Stratoclave/Ledger",
                "Dimensions": [[]],
                "Metrics": [
                    {"Name": "ReserveShadowDivergence"},
                    {"Name": "PostEpochSourcelessHolds"},
                ],
            }],
            "Timestamp": ts_ms,
        },
        "ReserveShadowDivergence": total_divergence,
        "PostEpochSourcelessHolds": post_epoch_sourceless,
        "detail": worst[:20],  # cap the log line
        "epoch_check_detail": epoch_check,
    }
    print(json.dumps(result))
    return result
