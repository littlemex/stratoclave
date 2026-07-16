"""
Property-based verification of the P0-13/14 rollup arithmetic (Hypothesis).

WHY HYPOTHESIS, NOT Z3, HERE: the risk is not an interleaving — DynamoDB ADD
on a single item is atomic and commutative by AWS contract — the risk is OUR
delta-derivation (`rollup_delta`) drifting from the per-span records, and our
pure model of ADD drifting from real UpdateItem semantics (missing attribute
treated as 0).  Both are data properties over unbounded values; Hypothesis
samples them far more usefully than an SMT encoding of integer addition.

Properties:
  P1. Folding rollup deltas is permutation-invariant (order of concurrent
      request finalizations cannot change the rollup).
  P2. Rollup counters agree with the span records they summarize (span_count,
      token sums, completed/error/canceled/partial counts).
  P3. (moto, optional) Real DynamoDB UpdateItem ADD matches the pure fold —
      including attribute-creation-as-zero on the first write.
  P4. emit_span_and_rollup never raises, even with no DynamoDB reachable.
"""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from mvp._converse_types import UsageAccumulator
from mvp.observability.store import (
    TERMINAL_STATUSES,
    SpanDraft,
    _safe_run_id,
    emit_span_and_rollup,
    rollup_delta,
)


# ---------------------------------------------------------------------------
# F2 defence-in-depth: run id can never blow the key-size limit or inject '#'.
# ---------------------------------------------------------------------------

@given(run_id=st.text(max_size=4000))
def test_safe_run_id_is_always_key_safe(run_id):
    safe = _safe_run_id(run_id)
    assert "#" not in safe                     # never injects our key delimiter
    assert 1 <= len(safe) <= 128               # never blows the 2048B PK limit
    # a clean, short id passes through verbatim; anything else is hashed stably
    import re as _re
    if (run_id and _re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", run_id)
            and not _re.fullmatch(r"h_[0-9a-f]{24}", run_id)):
        assert safe == run_id
    else:
        assert safe.startswith("h_")
        assert _safe_run_id(run_id) == safe    # deterministic


def test_safe_run_id_rehashes_the_hash_namespace():
    """Fable rev2 F2: a legal client id shaped like the hashed fallback must be
    re-hashed, so it cannot collide into another run's hashed item collection."""
    import hashlib
    squatter = "h_" + hashlib.sha1(b"anything").hexdigest()[:24]  # legal grammar
    out = _safe_run_id(squatter)
    assert out != squatter                       # not passed through (re-hashed)
    assert out.startswith("h_")
    assert _safe_run_id(squatter) == out         # deterministic for a given input
    # a genuinely fresh clean id still passes through unchanged
    assert _safe_run_id("run-abc.v2") == "run-abc.v2"


# ---------------------------------------------------------------------------
# Pure model of DynamoDB `ADD` on one item: missing attribute == 0.
# ---------------------------------------------------------------------------

def ddb_add(item: dict, deltas: dict) -> dict:
    out = dict(item)
    for k, v in deltas.items():
        out[k] = int(out.get(k, 0)) + int(v)
    return out


def fold(spans) -> dict:
    rollup: dict = {}
    for status, acc in spans:
        rollup = ddb_add(rollup, rollup_delta(status, acc))
    return rollup


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

acc_st = st.builds(
    UsageAccumulator,
    input_tokens=st.integers(min_value=0, max_value=2_000_000),
    output_tokens=st.integers(min_value=0, max_value=2_000_000),
    cache_read_tokens=st.integers(min_value=0, max_value=2_000_000),
    cache_write_tokens=st.integers(min_value=0, max_value=2_000_000),
    stop_reason=st.sampled_from([None, "end_turn", "max_tokens", "tool_use"]),
    saw_final_usage=st.booleans(),
)
span_st = st.tuples(st.sampled_from(TERMINAL_STATUSES), acc_st)
spans_st = st.lists(span_st, min_size=0, max_size=25)


# ---------------------------------------------------------------------------
# P1: ADD-fold is permutation-invariant.
# ---------------------------------------------------------------------------

@given(spans=spans_st, rnd=st.randoms(use_true_random=False))
def test_rollup_fold_is_permutation_invariant(spans, rnd):
    shuffled = list(spans)
    rnd.shuffle(shuffled)
    assert fold(spans) == fold(shuffled)


# ---------------------------------------------------------------------------
# P2: rollup counters == aggregate over the span records.
# ---------------------------------------------------------------------------

@given(spans=spans_st)
def test_rollup_agrees_with_span_records(spans):
    rollup = fold(spans)
    assert rollup.get("span_count", 0) == len(spans)
    assert rollup.get("input_tokens", 0) == sum(a.input_tokens for _, a in spans)
    assert rollup.get("output_tokens", 0) == sum(a.output_tokens for _, a in spans)
    assert rollup.get("cache_read_tokens", 0) == sum(a.cache_read_tokens for _, a in spans)
    assert rollup.get("cache_write_tokens", 0) == sum(a.cache_write_tokens for _, a in spans)
    # Fable F1: a client_disconnect WITH terminal usage (post-[DONE] close) is a
    # completed stream, not a cancel — so completed/canceled are split on
    # saw_final_usage, not on the raw status.
    def _completed(s, a):
        return s == "completed" or (s == "client_disconnect" and a.saw_final_usage)

    def _true_cancel(s, a):
        return s == "client_disconnect" and not a.saw_final_usage

    assert rollup.get("completed_count", 0) == sum(
        1 for s, a in spans if _completed(s, a))
    assert rollup.get("error_count", 0) == sum(
        1 for s, _ in spans if s in ("invoke_error", "midstream_error"))
    assert rollup.get("canceled_count", 0) == sum(
        1 for s, a in spans if _true_cancel(s, a))
    # usage_is_partial = not saw_final_usage (P0-14)
    assert rollup.get("partial_usage_count", 0) == sum(
        1 for _, a in spans if not a.saw_final_usage)


@given(status=st.sampled_from(TERMINAL_STATUSES), acc=acc_st)
def test_single_delta_shape_is_stable(status, acc):
    """The delta always ADDs the same fixed key set (a status must never make
    a counter disappear — that would break commutativity of the fold) and
    exactly one of completed/error/canceled is incremented."""
    d = rollup_delta(status, acc)
    assert d["span_count"] == 1
    assert all(isinstance(v, int) and v >= 0 for v in d.values())
    assert d["completed_count"] + d["error_count"] + d["canceled_count"] == 1
    assert set(d.keys()) == set(rollup_delta("completed", acc).keys())


def test_disconnect_after_done_counts_as_completed_not_canceled():
    """Fable F1: a client closing the socket right after [DONE] reaches the
    disconnect finally with saw_final_usage=True — a COMPLETED stream, not a
    cancel. This is run_stream's most common finalizer, so mislabelling it
    would structurally deflate completed_count."""
    acc = UsageAccumulator(output_tokens=5, saw_final_usage=True)
    d = rollup_delta("client_disconnect", acc)
    assert d["canceled_count"] == 0
    assert d["completed_count"] == 1
    assert d["partial_usage_count"] == 0


def test_disconnect_midstream_is_a_real_cancel():
    """A disconnect BEFORE terminal usage is a genuine mid-stream cancel."""
    acc = UsageAccumulator(output_tokens=2, saw_final_usage=False)
    d = rollup_delta("client_disconnect", acc)
    assert d["canceled_count"] == 1
    assert d["completed_count"] == 0
    assert d["partial_usage_count"] == 1


# ---------------------------------------------------------------------------
# P3 (optional): real DynamoDB ADD (moto) matches the pure model.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    pytest.importorskip("moto", reason="moto not installed") is None, reason=""
)
@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(spans=st.lists(span_st, min_size=1, max_size=8))
def test_real_dynamodb_add_matches_pure_model(spans):
    import boto3
    from moto import mock_aws

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="obs-prop-test",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        key = {"pk": "TENANT#t#RUN#r", "sk": "ROLLUP"}
        for status, acc in spans:
            delta = rollup_delta(status, acc)
            table.update_item(
                Key=key,
                UpdateExpression="ADD " + ", ".join(f"#{k} :{k}" for k in delta),
                ExpressionAttributeNames={f"#{k}": k for k in delta},
                ExpressionAttributeValues={f":{k}": v for k, v in delta.items()},
            )
        item = table.get_item(Key=key)["Item"]
        for k, v in fold(spans).items():
            assert int(item.get(k, 0)) == v, f"counter {k} diverged from pure model"


# ---------------------------------------------------------------------------
# P4: the writer NEVER raises into the caller — even with no DynamoDB, a
# broken resource factory, or a saturated queue.
# ---------------------------------------------------------------------------

def _draft() -> SpanDraft:
    return SpanDraft(
        tenant_id="t-unit", request_id="req_x", span_id="span_x",
        group_id=None, workflow_run_id=None, model_alias="alias",
        committed_model_id="m", committed_region="r",
        breaker_stage="closed", attempts_total=1, targets_distinct=1,
        stream=True, started_at_ms=0,
    )


def test_emit_never_raises_without_dynamodb(monkeypatch):
    import mvp.observability.store as store

    def _boom():
        raise RuntimeError("no dynamodb here")

    # Break the resource factory the worker uses; emit must still return None.
    monkeypatch.setattr(store, "_get_table", _boom, raising=True)
    acc = UsageAccumulator(output_tokens=5, saw_final_usage=True)
    assert emit_span_and_rollup(_draft(), "completed", acc) is None  # no raise

    # Saturate the admission semaphore: emit must DROP, not block or raise.
    while store._slots.acquire(blocking=False):
        pass
    try:
        assert emit_span_and_rollup(_draft(), "client_disconnect", acc) is None
    finally:
        # Restore capacity for other tests in the session.
        for _ in range(store._MAX_WORKERS + store._MAX_QUEUED):
            try:
                store._slots.release()
            except ValueError:
                break
