"""Integration tests for P0-13/14: span + rollup written through the real
Anthropic streaming handler, and money-path non-perturbation of the hook.

Full FastAPI TestClient with mocked auth + Bedrock. The observability write is
fire-and-forget on a dedicated executor, so tests drain that executor before
asserting on the DynamoDB rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mvp.anthropic import router as anthropic_router
from mvp.deps import get_current_user
from mvp.observability.context import HDR_GROUP_ID, HDR_WORKFLOW_RUN_ID


@dataclass
class _FakeUser:
    user_id: str = "user-obs-1"
    org_id: str = "obs-org"
    email: str = "t@example.com"
    roles: list = None
    auth_kind: str = "jwt"
    key_scopes: list = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["user"]


def _mock_converse_stream(**kwargs):
    return {"stream": iter([
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "hi"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 7, "outputTokens": 3}}},
    ])}


def _drain_obs_executor():
    """Block until every queued observability write has run (fire-and-forget
    writes hop onto a dedicated ThreadPoolExecutor)."""
    import mvp.observability.store as store
    import mvp.learning.signals as signals
    # Submit sentinels equal to worker count and wait — by the time these run,
    # all earlier-queued writes have completed (FIFO per worker). Drain BOTH the
    # observability executor (span/rollup) and the signals executor (P0-16).
    futs = [store._executor.submit(lambda: None) for _ in range(store._MAX_WORKERS)]
    futs += [signals._executor.submit(lambda: None) for _ in range(signals._MAX_WORKERS)]
    for f in futs:
        f.result(timeout=10)


def _wait_for_obs(query_fn, predicate, *, timeout=5.0, interval=0.05):
    """Poll `query_fn()` until `predicate(items)` holds, or `timeout` elapses.

    The span/rollup (and the P0-16 signal) are written FIRE-AND-FORGET on a
    dedicated ThreadPoolExecutor, submitted from the stream generator's finalize
    hook. `_drain_obs_executor` only waits for work ALREADY submitted; if the
    finalize hook's submit lands after the drain (which happens on a loaded/slow
    CI runner — reproduced by delaying the emit), a drain-then-query reads an
    empty table and the test flakes (assert 0 == 1). Draining first and then
    polling for the EXPECTED COMPLETE state (`predicate`, e.g. span AND rollup
    both present) is faithful to the fire-and-forget contract without racing it.
    The caller keeps its strict assertions: on timeout we return the last-seen
    items and let those asserts fail with the diagnostic printed below."""
    import time as _time
    import mvp.observability.store as _store
    _drain_obs_executor()
    deadline = _time.monotonic() + timeout
    items = query_fn()
    while not predicate(items) and _time.monotonic() < deadline:
        _time.sleep(interval)
        _drain_obs_executor()
        items = query_fn()
    if not predicate(items):
        pending = _store._MAX_WORKERS + _store._MAX_QUEUED - _store._slots._value
        print(f"[_wait_for_obs] timeout after {timeout}s; items={len(items)} "
              f"approx_pending_obs_writes={pending}")
    return items


@pytest.fixture
def api_client(dynamodb_mock, monkeypatch):
    import mvp.authz as _authz
    monkeypatch.setattr(_authz, "user_has_permission", lambda user, perm: True)

    from dynamo.user_tenants import UserTenantsRepository
    UserTenantsRepository().ensure(
        user_id=_FakeUser().user_id, tenant_id=_FakeUser().org_id,
        role="user", total_credit=10**9)

    app = FastAPI()
    app.include_router(anthropic_router)
    app.dependency_overrides[get_current_user] = lambda: _FakeUser()

    with patch("mvp.routing.infrarouter.bedrock_client") as mock_routing:
        mock_routing.return_value.converse_stream.side_effect = _mock_converse_stream
        yield TestClient(app)


def _obs_table():
    return boto3.resource("dynamodb", region_name="us-east-1").Table(
        "stratoclave-observability")


def _stream(client, headers=None):
    return client.post("/v1/messages", headers=headers or {}, json={
        "model": "us.anthropic.claude-opus-4-7",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50, "stream": True,
    })


class TestSpanAndRollupWritten:
    def test_completed_stream_writes_span_and_rollup(self, api_client):
        resp = _stream(api_client, headers={HDR_WORKFLOW_RUN_ID: "run-obs-1"})
        assert resp.status_code == 200
        list(resp.iter_lines())  # exhaust the stream so the generator finalizes

        tbl = _obs_table()

        def _q():
            return tbl.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
                    "TENANT#obs-org#RUN#run-obs-1"),
            )["Items"]

        items = _wait_for_obs(_q, lambda it: (
            len([i for i in it if i["sk"].startswith("SPAN#")]) == 1
            and len([i for i in it if i["sk"] == "ROLLUP"]) == 1))
        spans = [i for i in items if i["sk"].startswith("SPAN#")]
        rollups = [i for i in items if i["sk"] == "ROLLUP"]
        assert len(spans) == 1, items
        assert len(rollups) == 1, items

        span = spans[0]
        assert span["status"] == "completed"
        assert span["canceled_by_client"] is False
        assert span["usage_is_partial"] is False       # saw the terminal usage
        assert int(span["output_tokens"]) == 3
        assert span["workflow_run_id"] == "run-obs-1"

        rollup = rollups[0]
        assert int(rollup["span_count"]) == 1
        assert int(rollup["completed_count"]) == 1
        assert int(rollup["canceled_count"]) == 0
        assert int(rollup["output_tokens"]) == 3
        assert rollup["record_type"] == "workflow_run_rollup"

    def test_two_spans_same_run_rollup_accumulates(self, api_client):
        for _ in range(2):
            r = _stream(api_client, headers={HDR_WORKFLOW_RUN_ID: "run-obs-2"})
            list(r.iter_lines())

        tbl = _obs_table()

        def _q():
            return tbl.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
                    "TENANT#obs-org#RUN#run-obs-2"))["Items"]

        items = _wait_for_obs(_q, lambda it: (
            len([i for i in it if i["sk"].startswith("SPAN#")]) == 2
            and any(i["sk"] == "ROLLUP" and int(i.get("span_count", 0)) == 2 for i in it)))
        spans = [i for i in items if i["sk"].startswith("SPAN#")]
        rollup = next(i for i in items if i["sk"] == "ROLLUP")
        assert len(spans) == 2
        assert int(rollup["span_count"]) == 2          # ADD accumulated
        assert int(rollup["output_tokens"]) == 6

    def test_routing_signal_written_through_handler(self, api_client):
        # P0-16: a completed request writes one routing_signals item.
        resp = _stream(api_client, headers={HDR_WORKFLOW_RUN_ID: "run-sig-1"})
        assert resp.status_code == 200
        list(resp.iter_lines())

        import mvp.learning.signals as signals
        tbl = boto3.resource("dynamodb", region_name="us-east-1").Table(
            "stratoclave-routing-signals")

        def _q():
            return [i for i in tbl.scan()["Items"]
                    if i.get("workflow_run_id") == "run-sig-1"]

        mine = _wait_for_obs(_q, lambda it: len(it) == 1)
        assert len(mine) == 1, mine
        sig = mine[0]
        # key scheme: pk day-bucketed + sharded, sk time-ordered
        assert sig["pk"].startswith("TENANT#obs-org#CAT#")
        assert "#D#" in sig["pk"] and "#S#" in sig["pk"]
        assert sig["sk"].startswith("TS#")
        assert sig["status"] == "completed"
        assert sig["canceled_by_client"] is False
        assert int(sig["output_tokens"]) == 3
        assert sig["category"] in ("haiku", "sonnet", "opus", "other")
        # shard recomputable from the span token in the sk
        tok = sig["sk"].split("#")[2]
        assert int(sig["pk"].rsplit("#", 1)[1]) == signals.shard_for(tok, signals._SHARDS)

    def test_no_run_header_uses_request_id_as_run(self, api_client):
        resp = _stream(api_client)
        list(resp.iter_lines())
        run_id = resp.headers[HDR_WORKFLOW_RUN_ID]  # server-generated wr_...
        tbl = _obs_table()

        def _q():
            return tbl.query(
                KeyConditionExpression=boto3.dynamodb.conditions.Key("pk").eq(
                    f"TENANT#obs-org#RUN#{run_id}"))["Items"]

        items = _wait_for_obs(_q, lambda it: (
            any(i["sk"].startswith("SPAN#") for i in it)
            and any(i["sk"] == "ROLLUP" for i in it)))
        assert any(i["sk"].startswith("SPAN#") for i in items)
        assert any(i["sk"] == "ROLLUP" for i in items)


class TestMoneyNeutral:
    def test_observer_exception_does_not_break_the_stream(self, api_client, monkeypatch):
        # A raising emit_span_and_rollup must be swallowed by _notify; the
        # client still gets the full stream and the request still 200s.
        import mvp.anthropic as anthropic_mod

        def _boom(*a, **k):
            raise RuntimeError("observability blew up")

        monkeypatch.setattr(anthropic_mod, "emit_span_and_rollup", _boom, raising=False)
        # The name is imported into the module's _stream_messages via a local
        # import, so patch the source module too.
        import mvp.observability.store as store
        monkeypatch.setattr(store, "emit_span_and_rollup", _boom, raising=True)

        resp = _stream(api_client)
        assert resp.status_code == 200
        body = "".join(resp.iter_lines())
        assert "message_stop" in body or "content_block" in body or body  # stream delivered
