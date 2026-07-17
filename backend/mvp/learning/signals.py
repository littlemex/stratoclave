"""Routing-signals seam (P0-16) — write-only append log for offline learning.

LIVE in this increment: the writer (this module + the on_finalized wire).
STUBBED / deliberately absent: the consumer. Nothing reads this table; there
is no GSI, no DynamoDB Stream, and no query helper. The future offline
evaluator scans by (tenant, category, day, shard) — additive read-side work.

Guarantees (mirrors observability.store's P0-13/14 discipline):
  * emit_signal NEVER raises and NEVER blocks the event loop. Submit is a
    non-blocking bounded-semaphore acquire; on overflow the signal is DROPPED
    (signals are lossy-by-contract; the span record is the authoritative one).
  * All key inputs pass through observability.store._safe_key_token — same
    grammar, same hashed-fallback namespace, deliberately NOT a local copy
    (a divergent copy is exactly the F2-class bug the token exists to stop).
  * Own small executor, NOT the store's. Rationale: the span/rollup record is
    the authoritative billing-adjacent record; sharing slots would let a
    burst of best-effort signals starve span emits (and would require
    importing the store's private _slots, coupling shutdown behavior). Two
    workers is plenty: one PutItem per finalized request, no retries.

Key scheme:
  pk = TENANT#{tenant}#CAT#{category}#D#{yyyymmdd}#S#{shard}
  sk = TS#{created_at_ms:013d}#{span_id}
  shard = crc32(safe_span_token) % SC_SIGNAL_SHARDS   (env, default 8, clamp 1..64)
The shard is computed from the SAME safe token that appears in the sk, so a
consumer can recompute an item's full pk from the item alone. The 013d pad
keeps sk lexicographic order == time order until ~2286.
"""
from __future__ import annotations

import os
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from core.logging import get_logger
from ..observability.store import _safe_key_token  # single source of key grammar

logger = get_logger(__name__)

_TABLE_NAME = os.getenv("DYNAMODB_ROUTING_SIGNALS_TABLE", "stratoclave-routing-signals")


def _env_int_from(raw: Optional[str], *, default: int, lo: int, hi: int) -> int:
    """Defensive env parse: garbage -> default, then clamp. Split out so the
    parse rule itself is unit-testable without monkeypatching os.environ."""
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v))


def _env_int(name: str, *, default: int, lo: int, hi: int) -> int:
    return _env_int_from(os.getenv(name), default=default, lo=lo, hi=hi)


_SHARDS = _env_int("SC_SIGNAL_SHARDS", default=8, lo=1, hi=64)
_TTL_SECONDS = _env_int("SIGNAL_TTL_DAYS", default=90, lo=1, hi=3650) * 86_400

# Dedicated, bounded, drop-on-full executor (see module docstring for why
# this is NOT observability.store's executor).
_MAX_WORKERS = 2
_MAX_QUEUED = 128
_executor = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="signal-emit")
_slots = threading.BoundedSemaphore(_MAX_WORKERS + _MAX_QUEUED)


def shard_for(span_id: str, shards: int = _SHARDS) -> int:
    """Deterministic N-way shard over the (already key-safe) span token."""
    return zlib.crc32((span_id or "").encode("utf-8")) % max(1, int(shards))


def build_keys(
    *, tenant_id: str, category: str, span_id: str, created_at_ms: int,
    shards: int = _SHARDS,
) -> tuple[str, str]:
    """Pure key builder (property-tested). Every client-influenced value is
    sanitized, so no input can inject '#' or exceed the key byte limits."""
    t = _safe_key_token(tenant_id)
    c = _safe_key_token(category)
    s = _safe_key_token(span_id)
    day = time.strftime("%Y%m%d", time.gmtime(created_at_ms / 1000.0))
    pk = f"TENANT#{t}#CAT#{c}#D#{day}#S#{shard_for(s, shards)}"
    sk = f"TS#{created_at_ms:013d}#{s}"
    return pk, sk


def category_for_model(model_alias: str, committed_model_id: str = "") -> str:
    """Coarse learning category from the alias/model (mirrors chains._tier_for's
    vocabulary). Alias WINS over model id (F1: check the alias fully before the
    model id, else a cross-tier fallback whose alias says 'sonnet' but committed
    model says 'haiku' would mis-bucket to 'haiku' — and category is a pk
    dimension). Unknown -> 'other'."""
    for src in (model_alias or "", committed_model_id or ""):
        low = src.lower()
        for name in ("haiku", "sonnet", "opus"):
            if name in low:
                return name
    return "other"


def emit_signal_sync(
    *,
    tenant_id: str,
    group_id: str,
    workflow_run_id: str,
    span_id: str,
    category: str,
    committed_model_id: str,
    committed_region: str,
    cost_tier: int,
    chain_position_served: int,
    status: str,
    usage_is_partial: bool,
    canceled_by_client: bool,
    output_tokens: int,
    latency_first_event_ms: Optional[int],
    attempts_total: int,
    targets_distinct: int,
    breaker_stage: str,
) -> None:
    """One unconditional PutItem. Never raises. Runs on the signal executor,
    never on the event loop.

    The item stores `shards` (= the write-time SC_SIGNAL_SHARDS) so the future
    evaluator can enumerate S#0..N-1 for a (tenant, category, day) even if the
    env value later changes — otherwise historical N would be unknowable (F7)."""
    try:
        from dynamo.client import get_dynamodb_resource

        now_ms = int(time.time() * 1000)
        pk, sk = build_keys(
            tenant_id=tenant_id, category=category, span_id=span_id,
            created_at_ms=now_ms,
        )
        get_dynamodb_resource().Table(_TABLE_NAME).put_item(Item={
            "pk": pk,
            "sk": sk,
            "group_id": group_id or "",
            "workflow_run_id": workflow_run_id or "",
            "span_id": span_id,
            "category": category,
            "committed_model_id": committed_model_id,
            "committed_region": committed_region,
            "cost_tier": int(cost_tier),
            "chain_position_served": int(chain_position_served),
            "status": status,
            "usage_is_partial": bool(usage_is_partial),
            "canceled_by_client": bool(canceled_by_client),
            "output_tokens": int(output_tokens or 0),
            "latency_first_event_ms": int(latency_first_event_ms or 0),
            "attempts_total": int(attempts_total),
            "targets_distinct": int(targets_distinct),
            "breaker_stage": breaker_stage,
            "shards": int(_SHARDS),        # F7: write-time shard count for the consumer
            "created_at_ms": now_ms,
            # DynamoDB TTL is epoch SECONDS; env-tunable, defensively clamped.
            "expires_at": now_ms // 1000 + _TTL_SECONDS,
        })
    except Exception as e:  # noqa: BLE001 — fire-and-forget by contract
        try:
            logger.warning("routing_signal_write_failed", error=str(e))
        except Exception:
            pass


def _emit_guarded(kwargs: dict) -> None:
    try:
        emit_signal_sync(**kwargs)
    except Exception as e:  # noqa: BLE001 — a binding/arg TypeError here would
        # otherwise vanish into the discarded future with no trace.
        try:
            logger.warning("routing_signal_emit_failed", error=str(e))
        except Exception:
            pass
    finally:
        _slots.release()


def _submit(fn) -> None:
    """Generic fire-and-forget submit onto the shared bounded, drop-on-full
    executor. NEVER raises (not even BaseException) and NEVER blocks the event
    loop — the same contract as emit_signal. Used by the routing decision log
    (decision_log.py) so it shares this module's executor/backpressure rather
    than spinning up a second one. `fn` is a zero-arg callable that does the
    actual write and is expected to swallow its own errors; the wrapper releases
    the slot regardless."""
    def _guarded() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a stray error must not vanish silently.
            try:
                logger.warning("routing_submit_task_failed", error=str(e))
            except Exception:
                pass
        finally:
            _slots.release()

    try:
        if not _slots.acquire(blocking=False):
            logger.warning("routing_signal_dropped_queue_full")
            return
        try:
            _executor.submit(_guarded)
        except BaseException as e:  # noqa: BLE001
            _slots.release()
            try:
                logger.warning("routing_signal_submit_failed", error=str(e))
            except Exception:
                pass
    except BaseException as e:  # noqa: BLE001 — NEVER raises past this fence.
        try:
            logger.warning("routing_signal_submit_failed", error=str(e))
        except Exception:
            pass


def emit_signal(**kwargs) -> None:
    """Fire-and-forget, off the event loop, bounded, drop-on-full. NEVER raises
    — not even BaseException: a KeyboardInterrupt/SystemExit landing in submit()
    must not propagate into on_finalized (F5). Release the slot and swallow."""
    try:
        if not _slots.acquire(blocking=False):
            logger.warning("routing_signal_dropped_queue_full")
            return
        try:
            _executor.submit(_emit_guarded, dict(kwargs))
        except BaseException as e:  # noqa: BLE001 — submit failed; the worker
            # (which would release) never ran, so release here, then swallow
            # rather than re-raise past this never-raises boundary.
            _slots.release()
            try:
                logger.warning("routing_signal_submit_failed", error=str(e))
            except Exception:
                pass
    except BaseException as e:  # noqa: BLE001 — N3: the contract is NEVER raises,
        # not even BaseException (e.g. from _slots.acquire or the drop-path log);
        # swallow with a guarded log rather than escape both never-raises fences.
        try:
            logger.warning("routing_signal_submit_failed", error=str(e))
        except Exception:
            pass
