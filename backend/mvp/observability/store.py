"""Dual-track observability store (P0-13/14) — fire-and-forget, DynamoDB only.

Two writes per finalized request, both to ${prefix}-observability:

  TRACK 1 (span record, PutItem — immutable fact):
    pk = TENANT#{tenant_id}#RUN#{run_id}     sk = SPAN#{started_at_ms:013d}#{span_id}
    where run_id = workflow_run_id or request_id (a bare request is a run of one).
    Carries the frozen routing facts (SpanDraft), the finalize status, the
    canceled_by_client flag, and usage_is_partial = not saw_final_usage.

  TRACK 2 (workflow_run rollup, UpdateItem ADD — commutative counters):
    pk = TENANT#{tenant_id}#RUN#{run_id}     sk = ROLLUP
    ADD is atomic and order-independent per item, so concurrent finalizers of
    the same run cannot lose updates (proved order-independent in
    tests/test_observability_property.py).  GSI1 keys are set once via
    if_not_exists so the rollup is discoverable per tenant-day WITHOUT GSI
    churn on every update (sparse: span items never carry gsi1pk).

GUARANTEES
  * NEVER raises into the caller.
  * NEVER blocks the request: a dedicated bounded ThreadPoolExecutor with an
    explicit admission semaphore; when full we DROP the emit and log — an
    observability write is never worth request latency.
  * READS money outcome only; writes nothing the settle path reads.

HOT-PARTITION NOTES
  * Span writes share the run's partition — bounded by spans-per-run, fine.
  * The ROLLUP item is a single-item ADD hot spot: a run finalizing >~1000
    req/s would throttle that ONE item.  Acceptable: fire-and-forget drops
    are lossy-by-design, counters merely undercount under pathological load.
  * gsi1pk is per tenant-per-day and only the rollup (one update per request,
    set-once keys) touches it — no unbounded single GSI partition.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger

logger = get_logger(__name__)

_TABLE_NAME = os.getenv("DYNAMODB_OBSERVABILITY_TABLE", "stratoclave-observability")


def _ttl_seconds() -> int:
    """TTL from OBSERVABILITY_TTL_DAYS, defensively parsed (Fable rev2 F1).

    A bad env value ("30d", "", "thirty") must NOT raise: this module is
    imported on the request path, and an import-time ``int()`` blow-up would
    500 every request — turning an observability config typo into a full
    outage. Fall back to 30 days and warn instead."""
    raw = os.getenv("OBSERVABILITY_TTL_DAYS", "30")
    try:
        return int(raw) * 86_400
    except (TypeError, ValueError):
        logger.warning("observability_ttl_days_invalid", value=str(raw))
        return 30 * 86_400


_TTL_SECONDS = _ttl_seconds()

# Bounded, dedicated executor.  We do NOT use the loop's default executor:
# disconnect-settle threads live there and observability must never compete
# with money writes for threads.
_MAX_WORKERS = 4
_MAX_QUEUED = 256
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="obs-emit")
_slots = threading.BoundedSemaphore(_MAX_WORKERS + _MAX_QUEUED)

TERMINAL_STATUSES = ("invoke_error", "midstream_error", "completed", "client_disconnect")


@dataclass(frozen=True)
class SpanDraft:
    """Routing facts frozen BEFORE/AT stream start; immutable so the finalize
    thread can read them without racing the request coroutine.

    tenant_id is ALWAYS auth-derived (RequestContext / AuthenticatedUser),
    never a client header.
    """

    tenant_id: str
    request_id: str
    span_id: str
    group_id: Optional[str]
    workflow_run_id: Optional[str]
    model_alias: str
    committed_model_id: str      # "" when routing never committed (invoke_error)
    committed_region: str
    breaker_stage: str           # RoutedStream.breaker_stage; "unknown" pre-commit
    attempts_total: int
    targets_distinct: int
    stream: bool
    started_at_ms: int


@dataclass(frozen=True)
class _AccSnapshot:
    """Immutable copy of UsageAccumulator taken ON THE EVENT LOOP at emit
    time, so the worker thread never reads a mutable object."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    stop_reason: Optional[str]
    saw_final_usage: bool


def _is_true_cancel(status: str, saw_final_usage: bool) -> bool:
    """A request is a genuine client cancellation only if the client went away
    BEFORE the stream finished (Fable F1).

    The disconnect `finally` is run_stream's MOST COMMON finalizer: standard SDK
    clients close the socket the instant they parse the terminal `[DONE]`/
    message_stop, so a fully-delivered stream reaches the `finally` with
    status=='client_disconnect' even though nothing was actually cancelled.
    Terminal usage having arrived (`saw_final_usage`) means the stream completed;
    only a disconnect WITHOUT it is a real mid-stream cancel. Without this guard
    the majority of successful streams are mislabelled canceled and
    `completed_count` is structurally deflated.
    """
    return status == "client_disconnect" and not saw_final_usage


def rollup_delta(status: str, acc) -> dict:
    """Pure delta-derivation for the rollup ADD.  MUST return the same key set
    for every status (fold commutativity — see test_single_delta_shape_is_stable),
    and MUST stay a pure function: the Hypothesis suite folds it directly."""
    saw_final = bool(getattr(acc, "saw_final_usage", False))
    true_cancel = _is_true_cancel(status, saw_final)
    # A post-[DONE] disconnect (saw_final and status==client_disconnect) counts
    # as a completed stream, not a cancel — so completed/canceled stay truthful.
    completed = status == "completed" or (status == "client_disconnect" and saw_final)
    return {
        "span_count": 1,
        "input_tokens": int(acc.input_tokens),
        "output_tokens": int(acc.output_tokens),
        "cache_read_tokens": int(acc.cache_read_tokens),
        "cache_write_tokens": int(acc.cache_write_tokens),
        "completed_count": 1 if completed else 0,
        "error_count": 1 if status in ("invoke_error", "midstream_error") else 0,
        "canceled_count": 1 if true_cancel else 0,
        "partial_usage_count": 0 if saw_final else 1,
    }


def emit_span_and_rollup(draft: SpanDraft, status: str, acc) -> None:
    """Fire-and-forget dual write.  NEVER raises, NEVER blocks.

    Called from run_stream's on_finalized hook — i.e. on the event loop (or a
    closing generator's finally).  Everything slow happens on _executor.
    """
    try:
        snap = _AccSnapshot(
            input_tokens=int(acc.input_tokens),
            output_tokens=int(acc.output_tokens),
            cache_read_tokens=int(acc.cache_read_tokens),
            cache_write_tokens=int(acc.cache_write_tokens),
            stop_reason=acc.stop_reason,
            saw_final_usage=bool(getattr(acc, "saw_final_usage", False)),
        )
        if not _slots.acquire(blocking=False):
            logger.warning(
                "observability_emit_dropped_queue_full",
                tenant_id=draft.tenant_id, request_id=draft.request_id,
            )
            return
        try:
            _executor.submit(_emit_guarded, draft, status, snap)
        except Exception:
            _slots.release()
            raise
    except Exception as e:  # absolute last line of defense
        try:
            logger.warning("observability_emit_enqueue_failed", error=str(e))
        except Exception:
            pass


def _emit_guarded(draft: SpanDraft, status: str, snap: _AccSnapshot) -> None:
    try:
        _emit_sync(draft, status, snap)
    except Exception as e:
        try:
            logger.warning(
                "observability_emit_failed",
                error=str(e), tenant_id=draft.tenant_id, request_id=draft.request_id,
            )
        except Exception:
            pass
    finally:
        _slots.release()


def _get_table():
    from dynamo.client import get_dynamodb_resource

    return get_dynamodb_resource().Table(_TABLE_NAME)


import re as _re

_KEY_TOKEN_OK = _re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")
# The hashed-fallback namespace. A legal client id could otherwise squat here
# and merge with a hashed run (Fable rev2 F2), so ids that ALREADY look hashed
# are re-hashed too — the only remaining collision is a real sha1 prefix clash.
_HASHED_SHAPE = _re.compile(r"\Ah_[0-9a-f]{24}\Z")


def _safe_key_token(token: str) -> str:
    """Return a DynamoDB-key-safe token: pass a short, grammar-clean value
    through verbatim, else map it to a stable `h_<sha1[:24]>`.

    Defence in depth over the P0-12 edge grammar (Fable rev2 F2/F4): applied to
    BOTH the run id (partition key) and the span id (sort key) so a future edge
    change or a direct caller can never blow the key-size limit or inject the
    '#' delimiter. Values already in the `h_...` namespace are re-hashed so a
    legal client id can't collide into a hashed run's item collection."""
    if token and _KEY_TOKEN_OK.match(token) and not _HASHED_SHAPE.match(token):
        return token
    import hashlib
    return "h_" + hashlib.sha1((token or "").encode("utf-8")).hexdigest()[:24]


# Back-compat alias for the run-id-specific call sites/tests.
_safe_run_id = _safe_key_token


def _emit_sync(draft: SpanDraft, status: str, snap: _AccSnapshot) -> None:
    table = _get_table()
    now = time.time()
    now_ms = int(now * 1000)
    expires_at = int(now) + _TTL_SECONDS
    # workflow_run_id is edge-validated (P0-12 grammar: <=64 chars, no '#'), but
    # this layer defends in depth so a future edge change (or a direct caller)
    # can never blow the 2048-byte DynamoDB partition-key limit or inject a '#'
    # into the key. Over-long/illegal run ids are hashed to a stable safe token.
    run_id = _safe_key_token(draft.workflow_run_id or draft.request_id)
    span_id = _safe_key_token(draft.span_id)          # F4: same guard on the SK
    pk = f"TENANT#{draft.tenant_id}#RUN#{run_id}"
    usage_is_partial = not snap.saw_final_usage
    canceled_by_client = _is_true_cancel(status, snap.saw_final_usage)
    # F3: a post-[DONE] disconnect is a completed stream, so the RECORD fields
    # (span status, rollup last_status) must agree with the counters — otherwise
    # dashboards grouping by `status` still see the majority mislabelled. Keep
    # the raw transport fact under `finalizer` for debugging.
    final_status = (
        "completed"
        if status == "client_disconnect" and snap.saw_final_usage
        else status
    )

    # --- TRACK 1: span record (independent try — a span failure must not
    # starve the rollup, and vice versa) --------------------------------
    try:
        table.put_item(Item={
            "pk": pk,
            "sk": f"SPAN#{draft.started_at_ms:013d}#{span_id}",
            "record_type": "span",
            "tenant_id": draft.tenant_id,
            "request_id": draft.request_id,
            "span_id": span_id,
            "group_id": draft.group_id or "",
            "workflow_run_id": draft.workflow_run_id or "",
            "model_alias": draft.model_alias,
            "committed_model_id": draft.committed_model_id,
            "committed_region": draft.committed_region,
            "breaker_stage": draft.breaker_stage,
            "attempts_total": draft.attempts_total,
            "targets_distinct": draft.targets_distinct,
            "stream": draft.stream,
            "status": final_status,                        # F3: reclassified
            "finalizer": status,                           # raw transport fact
            "canceled_by_client": canceled_by_client,     # P0-14 flag
            "usage_is_partial": usage_is_partial,          # not saw_final_usage
            "input_tokens": snap.input_tokens,
            "output_tokens": snap.output_tokens,
            "cache_read_tokens": snap.cache_read_tokens,
            "cache_write_tokens": snap.cache_write_tokens,
            "stop_reason": snap.stop_reason or "",
            "started_at_ms": draft.started_at_ms,
            "finalized_at_ms": now_ms,
            "duration_ms": max(0, now_ms - draft.started_at_ms),
            "expires_at": expires_at,
        })
    except Exception as e:
        logger.warning("observability_span_write_failed", error=str(e),
                       request_id=draft.request_id)

    # --- TRACK 2: rollup ADD (commutative counters) ---------------------
    delta = rollup_delta(status, snap)
    names = {f"#{k}": k for k in delta}
    values = {f":{k}": v for k, v in delta.items()}
    day = time.strftime("%Y%m%d", time.gmtime(now))
    names.update({
        "#g1pk": "gsi1pk", "#g1sk": "gsi1sk",
        "#first": "first_seen_ms", "#lastst": "last_status",
        "#lastms": "last_finalized_ms", "#exp": "expires_at",
        "#tid": "tenant_id", "#rid": "run_id", "#rt": "record_type",
    })
    values.update({
        # Set-once GSI keys (sparse: only the ROLLUP carries them; keys never
        # rewritten -> no GSI delete+insert churn per update).
        ":g1pk": f"TENANT#{draft.tenant_id}#DAY#{day}",
        ":g1sk": f"TS#{now_ms:013d}#RUN#{run_id}",
        ":first": now_ms,
        ":lastst": final_status,
        ":lastms": now_ms,
        ":exp": expires_at,
        ":tid": draft.tenant_id,
        ":rid": run_id,
        ":rt": "workflow_run_rollup",
    })
    table.update_item(
        Key={"pk": pk, "sk": "ROLLUP"},
        UpdateExpression=(
            "ADD " + ", ".join(f"#{k} :{k}" for k in delta)
            + " SET #g1pk = if_not_exists(#g1pk, :g1pk),"
            "       #g1sk = if_not_exists(#g1sk, :g1sk),"
            "       #first = if_not_exists(#first, :first),"
            "       #tid = :tid, #rid = :rid, #rt = :rt,"
            "       #lastst = :lastst, #lastms = :lastms, #exp = :exp"
        ),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
