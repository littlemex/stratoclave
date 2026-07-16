"""Unit tests for the observability span/rollup record fields (Fable rev2).

Exercises _emit_sync directly against moto to assert:
  F3 — a post-[DONE] disconnect (status=client_disconnect + saw_final_usage) is
       recorded as status="completed" (with the raw transport fact under
       `finalizer`) so record fields agree with the counters.
  F4 — an over-long / '#'-bearing span_id is hashed in the sort key.
  F1 — a bad OBSERVABILITY_TTL_DAYS env value never raises at import.
"""
from __future__ import annotations

import boto3
import pytest

from mvp.observability import store as S


def _draft(**over):
    base = dict(
        tenant_id="t1", request_id="req_1", span_id="req_1",
        group_id=None, workflow_run_id="run-1", model_alias="m",
        committed_model_id="cm", committed_region="us-east-1",
        breaker_stage="closed", attempts_total=1, targets_distinct=1,
        stream=True, started_at_ms=1_000,
    )
    base.update(over)
    return S.SpanDraft(**base)


def _snap(**over):
    base = dict(input_tokens=1, output_tokens=2, cache_read_tokens=0,
                cache_write_tokens=0, stop_reason="end_turn", saw_final_usage=True)
    base.update(over)
    return S._AccSnapshot(**base)


@pytest.fixture
def obs_table(dynamodb_mock):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-observability")


class TestF3StatusReclassified:
    def test_post_done_disconnect_recorded_as_completed(self, obs_table):
        d = _draft()
        S._emit_sync(d, "client_disconnect", _snap(saw_final_usage=True))
        item = obs_table.get_item(Key={
            "pk": "TENANT#t1#RUN#run-1",
            "sk": f"SPAN#{1000:013d}#req_1"})["Item"]
        assert item["status"] == "completed"          # reclassified
        assert item["finalizer"] == "client_disconnect"  # raw fact preserved
        assert item["canceled_by_client"] is False
        rollup = obs_table.get_item(Key={"pk": "TENANT#t1#RUN#run-1", "sk": "ROLLUP"})["Item"]
        assert rollup["last_status"] == "completed"

    def test_midstream_disconnect_stays_disconnect(self, obs_table):
        d = _draft(request_id="req_2", span_id="req_2")
        S._emit_sync(d, "client_disconnect", _snap(saw_final_usage=False))
        item = obs_table.get_item(Key={
            "pk": "TENANT#t1#RUN#run-1",
            "sk": f"SPAN#{1000:013d}#req_2"})["Item"]
        assert item["status"] == "client_disconnect"
        assert item["canceled_by_client"] is True


class TestF4SpanIdSanitized:
    def test_oversized_span_id_is_hashed_in_sk(self, obs_table):
        big = "s" * 2000
        d = _draft(span_id=big, workflow_run_id="run-f4")
        S._emit_sync(d, "completed", _snap())
        items = obs_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
                "TENANT#t1#RUN#run-f4"))["Items"]
        span = next(i for i in items if i["sk"].startswith("SPAN#"))
        # the raw 2000-char id never lands in the sort key; a short hash does
        assert big not in span["sk"]
        assert span["span_id"].startswith("h_")

    def test_hash_span_id_did_not_break_write(self, obs_table):
        d = _draft(span_id="has#hash", workflow_run_id="run-f4b")
        S._emit_sync(d, "completed", _snap())
        items = obs_table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
                "TENANT#t1#RUN#run-f4b"))["Items"]
        span = next(i for i in items if i["sk"].startswith("SPAN#"))
        # '#' in span_id must not corrupt the SPAN#<ts># delimiter structure
        assert span["sk"].count("#") == 2


def test_f1_bad_ttl_env_does_not_raise(monkeypatch):
    monkeypatch.setenv("OBSERVABILITY_TTL_DAYS", "30d")
    # _ttl_seconds must fall back, not raise (import-path safety).
    assert S._ttl_seconds() == 30 * 86_400
    monkeypatch.setenv("OBSERVABILITY_TTL_DAYS", "")
    assert S._ttl_seconds() == 30 * 86_400
    monkeypatch.setenv("OBSERVABILITY_TTL_DAYS", "7")
    assert S._ttl_seconds() == 7 * 86_400
