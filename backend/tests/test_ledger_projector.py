"""Tests for the Streams RESERVE-event projector (two-item migration step 1).

Cover the pure derivation (HOLD image → RESERVE event, byte-equivalent to the
synchronous builder for the reconciled fields), the shadow sk namespace, the
skip rules (non-HOLD / pre-enrichment), and the Lambda handler's idempotency +
partial-batch-failure contract against moto.
"""
from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from billing.ledger_projector import (
    SHADOW_PREFIX,
    diff_events,
    handler,
    is_hold_record,
    reconcile_partition,
    reserve_event_from_hold,
)
from dynamo.credit_ledger import EV_RESERVE, ledger_pk, reserve_sk


def _hold_image(hold_id="h1", tenant_id="t1", period="2026-07", amount=2_000_000,
                source="external", run_id="run-1", desc="widget",
                rate=None, fallback=False):
    img = {
        "tenant_id": {"S": tenant_id},
        "sk": {"S": f"HOLD#{period}#0001700000000#{hold_id}"},
        "hold_id": {"S": hold_id},
        "period": {"S": period},
        "amount_microusd": {"N": str(amount)},
        "expires_at": {"N": "1700003600"},
        "created_at": {"S": "2026-07-19T00:00:00+00:00"},
    }
    if source:
        img["source"] = {"S": source}
    if run_id:
        img["run_id"] = {"S": run_id}
    if desc:
        img["hold_description"] = {"S": desc}
    if rate:
        img["rate_snapshot"] = {"S": rate}
    if fallback:
        img["run_id_source"] = {"S": "hold_id_fallback"}
    return img


def test_derives_reserve_event_from_enriched_hold():
    ev = reserve_event_from_hold(_hold_image())
    assert ev["event_type"]["S"] == EV_RESERVE
    assert ev["pk"]["S"] == ledger_pk("t1", "2026-07")
    assert ev["sk"]["S"] == reserve_sk("h1")
    assert ev["hold_id"]["S"] == "h1"
    assert ev["reserved_delta_microusd"]["N"] == "2000000"
    assert ev["settled_delta_microusd"]["N"] == "0"
    assert ev["source"]["S"] == "external"
    assert ev["run_id"]["S"] == "run-1"
    assert ev["description"]["S"] == "widget"


def test_shadow_prefixes_sk_only():
    ev = reserve_event_from_hold(_hold_image(), shadow=True)
    assert ev["sk"]["S"] == SHADOW_PREFIX + reserve_sk("h1")
    # event_id stays the real (un-shadowed) id so a reconciler can join on it.
    assert ev["event_id"]["S"] == reserve_sk("h1")[len("EV#"):]


def test_fallback_marker_preserved():
    ev = reserve_event_from_hold(_hold_image(run_id="h1", fallback=True))
    assert ev["run_id_source"]["S"] == "hold_id_fallback"


def test_skips_non_hold_and_pre_enrichment():
    budget_img = {"tenant_id": {"S": "t1"}, "sk": {"S": "BUDGET#2026-07"}}
    assert not is_hold_record(budget_img)
    assert reserve_event_from_hold(budget_img) is None
    # a HOLD with no `source` (pre-enrichment) is skipped, not projected.
    assert reserve_event_from_hold(_hold_image(source=None)) is None


def _make_ledger_table(ddb, name="stratoclave-credit-ledger"):
    ddb.create_table(
        TableName=name,
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                   {"AttributeName": "sk", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                              {"AttributeName": "sk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


def _insert_record(hold_id="h1", **kw):
    return {"eventName": "INSERT",
            "dynamodb": {"SequenceNumber": f"seq-{hold_id}",
                         "NewImage": _hold_image(hold_id=hold_id, **kw)}}


def test_handler_writes_shadow_event_idempotently(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("LEDGER_PROJECTOR_SHADOW", "true")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _make_ledger_table(ddb)
        from dynamo.client import get_dynamodb_resource
        get_dynamodb_resource.cache_clear()
        tbl = ddb.Table("stratoclave-credit-ledger")

        event = {"Records": [_insert_record("h1")]}
        out = handler(event)
        assert out["batchItemFailures"] == []
        got = tbl.get_item(Key={"pk": ledger_pk("t1", "2026-07"),
                                "sk": SHADOW_PREFIX + reserve_sk("h1")}).get("Item")
        assert got is not None and got["reserved_delta_microusd"] == 2_000_000

        # redeliver the SAME record → idempotent no-op, no failure.
        out2 = handler(event)
        assert out2["batchItemFailures"] == []
        # still exactly one shadow row.
        from boto3.dynamodb.conditions import Key
        rows = tbl.query(KeyConditionExpression=Key("pk").eq(ledger_pk("t1", "2026-07")))
        shadow_rows = [r for r in rows["Items"] if r["sk"].startswith(SHADOW_PREFIX)]
        assert len(shadow_rows) == 1


def test_handler_reports_partial_failure_on_bad_record(monkeypatch):
    """A record that raises during processing is returned in batchItemFailures
    (retried), never silently dropped; good records in the same batch still
    commit."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setenv("LEDGER_PROJECTOR_SHADOW", "true")
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _make_ledger_table(ddb)
        from dynamo.client import get_dynamodb_resource
        get_dynamodb_resource.cache_clear()

        good = _insert_record("good")
        # a record whose NewImage is malformed enough to raise inside the writer:
        bad = {"eventName": "INSERT",
               "dynamodb": {"SequenceNumber": "seq-bad",
                            "NewImage": _hold_image(hold_id="bad")}}
        # force a write error only for the bad record by making amount non-numeric
        bad["dynamodb"]["NewImage"]["amount_microusd"] = {"N": "not-a-number"}
        out = handler({"Records": [good, bad]})
        ids = [f["itemIdentifier"] for f in out["batchItemFailures"]]
        assert "seq-bad" in ids
        assert "seq-good" not in ids


# --------------------------------------------------------------------------
# Reconciler (migration step-1 gate): shadow vs synchronous RESERVE event.
# --------------------------------------------------------------------------

def _real_reserve_row(hold_id="h1", tenant_id="t1", period="2026-07",
                      amount=2_000_000, source="external", run_id="run-1",
                      desc="widget"):
    """A synchronous RESERVE event as the app writes it (resource-API shape)."""
    return {
        "pk": ledger_pk(tenant_id, period), "sk": reserve_sk(hold_id),
        "event_type": EV_RESERVE, "hold_id": hold_id,
        "tenant_id": tenant_id, "period": period,
        "reserved_delta_microusd": amount, "settled_delta_microusd": 0,
        "source": source, "run_id": run_id, "description": desc,
    }


def test_diff_events_matches_when_equal():
    real = _real_reserve_row()
    # a shadow projection of the same hold, resource-API shape
    shadow = dict(real)
    shadow["sk"] = SHADOW_PREFIX + real["sk"]
    assert diff_events(shadow, real) == {}


def test_diff_events_detects_field_diff():
    real = _real_reserve_row(amount=2_000_000)
    shadow = dict(real); shadow["reserved_delta_microusd"] = 999
    d = diff_events(shadow, real)
    assert "reserved_delta_microusd" in d and d["reserved_delta_microusd"] == (999, 2_000_000)


def _seed_ledger():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    _make_ledger_table(ddb)
    from dynamo.client import get_dynamodb_resource
    get_dynamodb_resource.cache_clear()
    return ddb.Table("stratoclave-credit-ledger")


def test_reconcile_partition_zero_divergence(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        real = _real_reserve_row("h1")
        tbl.put_item(Item=real)
        shadow = dict(real); shadow["sk"] = SHADOW_PREFIX + real["sk"]
        tbl.put_item(Item=shadow)
        summ = reconcile_partition(tbl, "t1", "2026-07")
        assert summ["divergence"] == 0
        assert summ["shadow_count"] == 1 and summ["real_count"] == 1


def test_reconcile_partition_flags_field_diff(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        real = _real_reserve_row("h1", amount=2_000_000)
        tbl.put_item(Item=real)
        shadow = dict(real); shadow["sk"] = SHADOW_PREFIX + real["sk"]
        shadow["reserved_delta_microusd"] = 111  # corrupt
        tbl.put_item(Item=shadow)
        summ = reconcile_partition(tbl, "t1", "2026-07")
        assert summ["divergence"] == 1
        assert "h1" in summ["field_diff"]


def test_reconcile_young_missing_shadow_is_lag_not_divergence(monkeypatch):
    """A synchronous RESERVE within the stream-lag budget with no shadow is
    benign lag (the projector just hasn't caught up), NOT divergence."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        row = _real_reserve_row("h1")
        row["ts_ms"] = 1_000_000          # a fixed event time
        tbl.put_item(Item=row)
        # now is 5 min after the event; lag budget 15 min → still lag.
        summ = reconcile_partition(tbl, "t1", "2026-07",
                                   now_ms=1_000_000 + 5 * 60 * 1000)
        assert summ["divergence"] == 0
        assert summ["lagging_shadow"] == ["h1"]
        assert summ["missing_shadow"] == []


def test_reconcile_stale_missing_shadow_is_divergence(monkeypatch):
    """Fable review finding 4: a synchronous RESERVE OLDER than the lag budget
    with no shadow means the projector PERMANENTLY dropped it — this MUST count as
    divergence, not be hidden as lag (else the cut-over gate is worthless)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        row = _real_reserve_row("h1")
        row["ts_ms"] = 1_000_000
        tbl.put_item(Item=row)
        # now is 30 min after the event; lag budget 15 min → stale = bug.
        summ = reconcile_partition(tbl, "t1", "2026-07",
                                   now_ms=1_000_000 + 30 * 60 * 1000)
        assert summ["divergence"] == 1
        assert summ["missing_shadow"] == ["h1"]


# --------------------------------------------------------------------------
# Scheduled reconciler handler (scan → divergence summary).
# --------------------------------------------------------------------------

def test_reconciler_handler_zero_divergence_across_partitions(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        # two partitions, each with matched real + shadow RESERVE
        for tid in ("t1", "t2"):
            real = _real_reserve_row("h1", tenant_id=tid)
            tbl.put_item(Item=real)
            shadow = dict(real); shadow["sk"] = SHADOW_PREFIX + real["sk"]
            tbl.put_item(Item=shadow)
        from billing.ledger_reconciler import handler as rec_handler
        out = rec_handler({})
        assert out["total_divergence"] == 0
        assert out["partitions"] == 2


def test_reconciler_handler_flags_divergence(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        real = _real_reserve_row("h1", amount=2_000_000)
        tbl.put_item(Item=real)
        shadow = dict(real); shadow["sk"] = SHADOW_PREFIX + real["sk"]
        shadow["reserved_delta_microusd"] = 7  # corrupt projection
        tbl.put_item(Item=shadow)
        from billing.ledger_reconciler import handler as rec_handler
        out = rec_handler({})
        assert out["total_divergence"] == 1
        assert out["ReserveShadowDivergence"] == 1


def test_reconcile_excludes_pre_epoch_backlog(monkeypatch):
    """A RESERVE minted before the projector's epoch has no shadow by
    construction (stream started at LATEST); it must be OUT OF DOMAIN, not
    divergence — else the historical backlog perpetually fails the gate."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    with mock_aws():
        tbl = _seed_ledger()
        # an old RESERVE (pre-epoch), no shadow
        old = _real_reserve_row("h_old"); old["ts_ms"] = 1_000_000
        tbl.put_item(Item=old)
        # a new in-domain RESERVE with its matching shadow
        new = _real_reserve_row("h_new"); new["ts_ms"] = 5_000_000
        tbl.put_item(Item=new)
        sh = dict(new); sh["sk"] = SHADOW_PREFIX + new["sk"]
        tbl.put_item(Item=sh)
        summ = reconcile_partition(tbl, "t1", "2026-07",
                                   now_ms=5_000_000 + 60_000,
                                   projector_epoch_ms=3_000_000)
        assert summ["divergence"] == 0          # old is out of domain, new matches
        assert summ["out_of_domain"] == 1
        assert summ["missing_shadow"] == []
