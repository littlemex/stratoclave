"""Session-Aware Agentic Routing (SAAR).

SAAR makes model selection a *session-level* decision instead of a per-turn one:
within one agent session it prefers to keep serving the same physical model
(prefix-cache locality + tool-loop correctness), and it prices a model *switch*
in real micro-USD against the same credit ledger the inline path uses, so a
switch that would breach the tenant's budget is gated exactly like any other
spend. The design principle (Fable SAAR design) is that **no new money operation
is created**: SAAR only changes the RESERVE estimate's magnitude and writes a
money-neutral routing claim; the reserve/settle/ledger/rating machinery is
untouched.

This module is the SAAR home. It is INERT unless ``SAAR_ENABLED=true`` on the
task — when off, ``saar_enabled()`` returns False and no routing-memory read or
write ever runs, so the request path is byte-identical to pre-SAAR Stratoclave
(the degenerate-safety invariant). SAAR is also per-tenant opt-in-able via the
routing config, but the global flag is the master switch.

Layering (all in this file so the state machine stays in one place):
  * feature flag + session-partition helpers   (P0-1, here)
  * SAARMEM router-memory store                 (P0-2)
  * the decision function                        (P0-3)
The RESERVE stay/switch estimate (P0-4) and the decision-log claim + replay
headers (P0-5) live at their existing call sites (`_pipeline`, the handlers) and
call into this module.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from botocore.exceptions import ClientError

from core.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# feature flag (master switch)
# ---------------------------------------------------------------------------


def saar_enabled() -> bool:
    """Master switch, checked at REQUEST time (not import) so a task can flip it
    via env without a code change — the same pattern as ``CODEX_ENABLED``.
    Defaults to **False**: SAAR ships dark, and a deployment must opt in. When
    False, callers must not touch routing memory at all (no GetItem, no PutItem),
    so the hot path pays exactly zero SAAR cost."""
    return os.getenv("SAAR_ENABLED", "false").lower() == "true"


# ---------------------------------------------------------------------------
# session partition (tenant-safe)
# ---------------------------------------------------------------------------

_MEM_PK_PREFIX = "SAARMEM"


def session_partition(tenant_id: str) -> str:
    """The routing-memory partition key for a tenant. The tenant component comes
    ONLY from the authenticated principal (never a client header), so a forged or
    guessed session id can at worst pollute the caller's OWN tenant's routing
    state — it can never address another tenant's partition (tenant-separation
    invariant)."""
    return f"{_MEM_PK_PREFIX}#{tenant_id}"


def session_sort_key(
    session_key: str,
    *,
    user_scoped: bool = False,
    user_id: Optional[str] = None,
) -> str:
    """The sort key for one session's routing memory. Normally ``SESSION#<id>``
    (shared across the tenant's users — the common single-agent case). When the
    tenant opts into ``saar_user_scoped``, the acting user's id is folded in so
    two users sharing (or guessing) a session id cannot perturb each other's
    continuity. The session id has already passed the correlation-id grammar
    (``#``-free), so it is a safe sk component."""
    if user_scoped and user_id:
        return f"SESSION#{user_id}#{session_key}"
    return f"SESSION#{session_key}"


# ---------------------------------------------------------------------------
# phases (a session's continuity state)
# ---------------------------------------------------------------------------


class Phase:
    """A session's routing phase. `tool_loop`/`provider_state` are HARD-lock
    phases (an unsafe model switch is forbidden regardless of cost); `normal` and
    `reset` permit reselection. Kept as plain string constants (not an Enum) so
    they round-trip through DynamoDB attributes without conversion."""

    NORMAL = "normal"
    TOOL_LOOP = "tool-loop"
    PROVIDER_STATE = "provider-state"
    RESET = "reset"

    HARD_LOCK = frozenset({TOOL_LOOP, PROVIDER_STATE})


@dataclass(frozen=True)
class SessionMemory:
    """The minimal routing state for one session — NOT conversation memory. It
    carries only what the next model-selection decision needs to be safe:

      * ``last_physical_model`` — the model the previous turn actually ran on;
        the sticky/lock target.
      * ``matched_decision``    — the routing-decision name the previous turn
        matched (for decision-drift reset).
      * ``phase``               — see :class:`Phase`.
      * ``switch_count`` / ``turn_count`` — monotonic counters (turn_count guards
        against stale concurrent writes).
      * ``last_turn_at``        — epoch seconds, for the idle-reset boundary.
      * ``warm_prefix_tokens`` / ``last_cache_write_at`` — cache evidence used to
        price a switch's prefix-cache checkout (populated on settle in P1; 0 in
        P0 so SAAR degenerates to cost-neutral stickiness until evidence exists).
      * ``rating_version``      — the rating version the last claim used, so a
        replayed claim recomputes the same micro-USD delta (provable).
      * ``minted_response_id``  — the provider continuation id the LAST turn
        minted (a Responses ``response_id``), bound to ``last_physical_model``.
        The provider-state lock fires ONLY when the next request's
        ``previous_response_id`` EQUALS this — so a client can never force a lock
        (or a wrong-backend lock) with an arbitrary/forged id, and the lock
        target is provably the backend that actually minted the state. Stored,
        not conversation content: it is an opaque routing token.
    """

    last_physical_model: str
    phase: str = Phase.NORMAL
    matched_decision: Optional[str] = None
    switch_count: int = 0
    turn_count: int = 0
    last_turn_at: int = 0
    warm_prefix_tokens: int = 0
    last_cache_write_at: int = 0
    rating_version: Optional[str] = None
    replay_id: Optional[str] = None
    minted_response_id: Optional[str] = None


# ---------------------------------------------------------------------------
# SAARMEM router-memory store (P0-2)
# ---------------------------------------------------------------------------

_TABLE_NAME = os.getenv("DYNAMODB_SAAR_MEMORY_TABLE", "stratoclave-saar-memory")

# Hot-path read budget. The decision runs inline before the Bedrock call, so a
# slow memory read must never blow the p99 authorization budget. We bound it the
# same way the DynamoDB rate limiter bounds its own hot read: a DEDICATED boto3
# client with short connect/read timeouts and NO retries. On any error/timeout
# the read fails OPEN to memory=None (⇒ the existing cascade, i.e. pre-SAAR
# behaviour). This is deliberately NOT a thread-pool wall-clock cap — sharing an
# executor would couple read latency to write-queue depth; the socket timeout is
# the honest, non-queueing bound.
def _env_float(name: str, *, default: float, lo: float, hi: float) -> float:
    """Fenced env-float parse: garbage ⇒ default, then clamp. Fenced so a typo'd
    env value (``SAAR_READ_TIMEOUT_S=abc``) can never make module import fail —
    which, at import time, would take the whole app down even with SAAR off
    (Fable SAAR review-1 M5)."""
    try:
        v = float(str(os.getenv(name)).strip())
    except (TypeError, ValueError, AttributeError):
        v = default
    return max(lo, min(hi, v))


# Kept well under the p99<50ms authorization budget: even a fully-stalled read
# (connect + read) is bounded to ~2×timeout and then fails open to the cascade.
# 40ms default (Fable SAAR review-1 H3 — 0.25s would have blown the budget).
_READ_TIMEOUT_S = _env_float("SAAR_READ_TIMEOUT_S", default=0.04, lo=0.005, hi=1.0)
# One session's routing state is worthless once the session is abandoned; a day
# is far beyond any realistic idle-reset window and keeps the table small.
_TTL_SECONDS = 86_400

_read_client = None
_read_client_lock = threading.Lock()


def _get_read_client():
    """A process-wide, short-timeout, no-retry DynamoDB client for the hot-path
    memory read (mirrors core.rate_limit_ddb's dedicated client). Lazily built so
    tests that swap the moto region/resource still work, and reset via
    ``reset_read_client`` between test cases."""
    global _read_client
    if _read_client is not None:
        return _read_client
    with _read_client_lock:
        if _read_client is None:
            import boto3
            from botocore.config import Config

            region = os.getenv("AWS_REGION", "us-east-1")
            _read_client = boto3.client(
                "dynamodb",
                region_name=region,
                config=Config(
                    connect_timeout=_READ_TIMEOUT_S,
                    read_timeout=_READ_TIMEOUT_S,
                    retries={"max_attempts": 1, "mode": "standard"},
                ),
            )
    return _read_client


def reset_read_client() -> None:
    """Drop the cached hot-read client (test hook, mirrors rate_limit_ddb)."""
    global _read_client
    _read_client = None


def load_session_memory(
    *,
    tenant_id: str,
    session_key: str,
    user_scoped: bool = False,
    user_id: Optional[str] = None,
) -> Optional[SessionMemory]:
    """Read one session's routing memory. HOT PATH: a bounded, single point
    GetItem. Returns None on miss OR on any error/timeout (fail-open) — the
    caller then behaves exactly as pre-SAAR (existing cascade). NEVER raises.

    Eventually-consistent read on purpose: the previous turn's write may not have
    propagated, in which case we simply lose one turn of stickiness (a cost blip,
    never a correctness break — the hard locks are re-derived from THIS request's
    content, not from stale memory alone)."""
    try:
        pk = session_partition(tenant_id)
        sk = session_sort_key(session_key, user_scoped=user_scoped, user_id=user_id)
        resp = _get_read_client().get_item(
            TableName=_TABLE_NAME,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=False,
        )
        item = resp.get("Item")
        if not item:
            return None
        return SessionMemory(
            last_physical_model=_s(item, "last_physical_model"),
            phase=_s(item, "phase") or Phase.NORMAL,
            matched_decision=_s(item, "matched_decision") or None,
            switch_count=_n(item, "switch_count"),
            turn_count=_n(item, "turn_count"),
            last_turn_at=_n(item, "last_turn_at"),
            warm_prefix_tokens=_n(item, "warm_prefix_tokens"),
            last_cache_write_at=_n(item, "last_cache_write_at"),
            rating_version=_s(item, "rating_version") or None,
            replay_id=_s(item, "replay_id") or None,
            minted_response_id=_s(item, "minted_response_id") or None,
        )
    except Exception as e:  # noqa: BLE001 — fail-open: a memory read must never
        # break routing (a timeout, a throttle, a missing table). Degrade to the
        # existing cascade = pre-SAAR behaviour.
        try:
            logger.warning("saar_memory_read_failed", error=str(e))
        except Exception:
            pass
        return None


def _s(item: dict, key: str) -> str:
    """Read a low-level String attribute (``{"S": ...}``); "" if absent."""
    v = item.get(key)
    return v.get("S", "") if isinstance(v, dict) else ""


def _n(item: dict, key: str) -> int:
    """Read a low-level Number attribute (``{"N": "..."}``); 0 if absent/bad."""
    v = item.get(key)
    if isinstance(v, dict) and "N" in v:
        try:
            return int(v["N"])
        except (TypeError, ValueError):
            return 0
    return 0


def save_session_memory(
    *,
    tenant_id: str,
    session_key: str,
    mem: SessionMemory,
    user_scoped: bool = False,
    user_id: Optional[str] = None,
) -> None:
    """Persist a session's routing state AFTER the response is settled. Fire-and-
    forget on the shared telemetry executor (never blocks the event loop, never
    raises). A monotonic ``turn_count`` guard makes concurrent turns writing the
    same session converge to the highest turn rather than flapping (an older
    in-flight turn's write ConditionalCheckFails and is dropped). NOT part of any
    ledger transaction — money is never touched by this write (money-neutrality
    invariant)."""
    from ..learning.signals import _submit

    def _write() -> None:
        try:
            from dynamo.client import get_dynamodb_resource

            pk = session_partition(tenant_id)
            sk = session_sort_key(session_key, user_scoped=user_scoped, user_id=user_id)
            now = int(mem.last_turn_at or 0)
            ttl_base = now if now else int(time.time())
            item = {
                "pk": pk,
                "sk": sk,
                "last_physical_model": mem.last_physical_model,
                "phase": mem.phase,
                "switch_count": int(mem.switch_count),
                "turn_count": int(mem.turn_count),
                "last_turn_at": now,
                "warm_prefix_tokens": int(mem.warm_prefix_tokens),
                "last_cache_write_at": int(mem.last_cache_write_at),
                "ttl": ttl_base + _TTL_SECONDS,
            }
            if mem.matched_decision is not None:
                item["matched_decision"] = mem.matched_decision
            if mem.rating_version is not None:
                item["rating_version"] = mem.rating_version
            if mem.replay_id is not None:
                item["replay_id"] = mem.replay_id
            if mem.minted_response_id is not None:
                item["minted_response_id"] = mem.minted_response_id
            get_dynamodb_resource().Table(_TABLE_NAME).put_item(
                Item=item,
                # monotonic-writer-wins: only advance the item if this turn is
                # newer than what's stored (or the item is new).
                ConditionExpression=(
                    "attribute_not_exists(turn_count) OR turn_count < :t"
                ),
                ExpressionAttributeValues={":t": int(mem.turn_count)},
            )
        except ClientError as ce:
            # A stale-turn ConditionalCheckFailed is the EXPECTED loser-drop
            # (monotonic-writer-wins), not an error — swallow it silently. Any
            # other ClientError is a real (but non-fatal, fire-and-forget) write
            # failure worth a guarded log.
            if ce.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                try:
                    logger.warning("saar_memory_write_failed", error=str(ce))
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001 — fire-and-forget must never surface.
            try:
                logger.warning("saar_memory_write_failed", error=str(e))
            except Exception:
                pass

    _submit(_write)


# ---------------------------------------------------------------------------
# decision (P0-3)
# ---------------------------------------------------------------------------

# Idle-reset boundary: a session untouched for longer than this is treated as a
# fresh session (continuity locks and cache evidence are discarded, reselection
# is allowed). Per Fable design / vLLM SAAR default (300s). Tunable per tenant
# later (P1); a single env default for P0.
_IDLE_RESET_SECONDS = int(_env_float("SAAR_IDLE_RESET_SECONDS", default=300, lo=1, hi=86_400))

# Provider-state hard cap: the MAXIMUM age a provider-state lock may reach before
# it yields to idle reset. A provider-state lock outlives the normal idle boundary
# (a continuation id stays bound to its backend), but NOT forever — past this cap
# the session is freed even if the client keeps replaying the id, so a retired /
# permanently-broken backend can never strand a session (Fable review §1 escape
# hatch). Default 1h: comfortably longer than any live continuation, far short of
# "forever". Bounded like the idle boundary.
_PROVIDER_STATE_HARD_CAP_SECONDS = int(
    _env_float("SAAR_PROVIDER_STATE_HARD_CAP_SECONDS", default=3600, lo=1, hi=86_400)
)


@dataclass(frozen=True)
class SaarDecision:
    """The outcome of the SAAR pre-pass. ``hard_model`` (when set) is fed to
    ``resolve_model`` as ``vsr_hard_model`` — a session-derived pin that disables
    cascade for this turn. ``None`` means "SAAR has no opinion; use the normal
    cascade". ``reason``/``phase``/``switched`` are for the replay trace and the
    decision-log claim; they never affect money on their own."""

    # A HARD lock (correctness): force this exact model, disable cascade. Set
    # ONLY by the tool-loop lock, where sending the tool result to a different
    # model would break the loop. None everywhere else.
    hard_model: Optional[str]
    # A SOFT preference (cost/locality): prefer this model at the HEAD of the
    # cascade, but fall through to the normal fallback chain if it is disallowed /
    # breaker-capped / quota-exhausted. This is what "sticky" is — a preference,
    # never a hard pin — so SAAR can never turn a request that pre-SAAR would have
    # served (via cascade) into a 403/402/429 (Fable SAAR review-1 C2).
    prefer_model: Optional[str]
    phase: str                         # resulting phase (persisted for next turn)
    reason: str                        # 'sticky'|'tool-loop-lock'|'provider-state-lock'|'reset'|'drift'|'cold'|'disabled'
    switched: bool                     # True iff the committed model differs from prev (set post-settle)
    prev_model: Optional[str] = None   # last_physical_model from memory (for stay/switch pricing)
    warm_prefix_tokens: int = 0        # cache evidence carried for the RESERVE estimate (P0: 0)
    stale: bool = False                # idle-reset fired ⇒ cache evidence discarded

    @property
    def acted(self) -> bool:
        """True iff SAAR expressed an opinion this turn (a hard lock or a soft
        preference). A cold/reset/drift decision is 'no opinion' — the handler
        still emits replay headers to keep the audit trail complete, but callers
        that only care whether routing was steered check this."""
        return bool(self.hard_model or self.prefer_model)


def decide(
    *,
    mem: Optional[SessionMemory],
    now_epoch: int,
    request_has_tool_result: bool,
    request_provider_state_id: Optional[str] = None,
    matched_decision: Optional[str] = None,
    idle_reset_seconds: int = _IDLE_RESET_SECONDS,
    provider_state_hard_cap_seconds: int = _PROVIDER_STATE_HARD_CAP_SECONDS,
) -> SaarDecision:
    """Pure SAAR decision (no I/O — memory is already loaded, persistence is the
    caller's). Precedence, highest first:

      1. No memory (miss / fail-open / first turn) ⇒ no opinion; the cascade
         decides. Degenerate-safe: a failed read is indistinguishable from a new
         session, and both mean "no continuity to preserve".
      2. Idle reset: idle past the boundary ⇒ discard continuity + cache evidence,
         no opinion (``reset`` phase). Does NOT fire for a VERIFIED, in-cap
         provider-state continuation (handled at 3a) — a non-portable id is bound
         to its origin backend regardless of elapsed time — but DOES fire once the
         provider-state hard cap is exceeded (a bounded escape hatch, below).
      3a. Provider-state HARD lock: stored phase ``provider-state`` AND this
         request's ``previous_response_id`` EXACTLY MATCHES the id the last turn
         minted (``mem.minted_response_id``) AND the lock is within its hard cap
         ⇒ the state lives ONLY on the backend that minted it ⇒ ``hard_model =
         last_physical_model`` (cascade disabled). Verified against the stored
         minted id, so a client can NEVER force a lock (or a wrong-backend lock)
         with an arbitrary/forged/foreign id. Checked before idle reset so a
         still-referenced continuation is not reset away — but bounded by a hard
         cap so a client that keeps replaying the same id can never strand the
         session on a dead backend forever (Fable provider-state review §1).
      3b. Tool-loop HARD lock: stored phase ``tool-loop`` AND this request carries
         a tool result ⇒ the result MUST return to the model that emitted the
         tool_use ⇒ ``hard_model = last_physical_model`` (cascade disabled).
      4. Decision drift (normal phase only): the matched routing decision changed
         ⇒ the task shape changed ⇒ no opinion, let the cascade reselect.
      5. Sticky-by-default (normal phase, no drift): ``prefer_model =
         last_physical_model`` — a SOFT preference that heads the cascade to keep
         prefix-cache locality but still falls through if that model is
         unavailable. This is the common case, and being soft is what makes SAAR
         unable to reduce availability (Fable review-1 C2)."""
    # 1. no memory ⇒ no opinion
    if mem is None or not mem.last_physical_model:
        return SaarDecision(hard_model=None, prefer_model=None, phase=Phase.NORMAL,
                            reason="cold", switched=False)

    prev = mem.last_physical_model
    idle = max(0, now_epoch - int(mem.last_turn_at or 0))

    # 3a. provider-state HARD lock — VERIFIED and BOUNDED. Fires only when the
    # request's previous_response_id equals the id THIS session's last turn minted
    # (never a forged/foreign id) AND the lock has not exceeded its hard cap.
    # Checked before idle reset so a live continuation is not reset away; the cap
    # is the escape hatch so a dead backend can't strand the session forever.
    provider_state_matches = bool(
        mem.phase == Phase.PROVIDER_STATE
        and mem.minted_response_id
        and request_provider_state_id
        and str(request_provider_state_id) == str(mem.minted_response_id)
    )
    within_cap = idle <= max(1, int(provider_state_hard_cap_seconds))
    if provider_state_matches and within_cap:
        return SaarDecision(
            hard_model=prev, prefer_model=None, phase=Phase.PROVIDER_STATE,
            reason="provider-state-lock", switched=False, prev_model=prev,
            warm_prefix_tokens=int(mem.warm_prefix_tokens),
        )

    # 2. idle reset ⇒ no opinion (a matched-but-over-cap provider-state lock also
    #    lands here — the bounded escape hatch)
    if mem.last_turn_at and idle > max(1, int(idle_reset_seconds)):
        return SaarDecision(
            hard_model=None, prefer_model=None, phase=Phase.RESET, reason="reset",
            switched=False, prev_model=prev, stale=True,
        )

    # 3b. tool-loop HARD lock (correctness — cost cannot override, cascade disabled)
    if mem.phase == Phase.TOOL_LOOP and request_has_tool_result:
        return SaarDecision(
            hard_model=prev, prefer_model=None, phase=Phase.TOOL_LOOP,
            reason="tool-loop-lock", switched=False, prev_model=prev,
            warm_prefix_tokens=int(mem.warm_prefix_tokens),
        )

    # 4. decision drift (normal only) ⇒ no opinion (cascade reselects)
    if (
        mem.phase == Phase.NORMAL
        and matched_decision is not None
        and mem.matched_decision is not None
        and matched_decision != mem.matched_decision
    ):
        return SaarDecision(
            hard_model=None, prefer_model=None, phase=Phase.NORMAL, reason="drift",
            switched=False, prev_model=prev,
        )

    # 5. sticky-by-default: SOFT-prefer the warm model (cascade still available)
    return SaarDecision(
        hard_model=None, prefer_model=prev, phase=Phase.NORMAL, reason="sticky",
        switched=False, prev_model=prev,
        warm_prefix_tokens=int(mem.warm_prefix_tokens),
    )


def replay_headers(
    *,
    replay_id: str,
    decision: "SaarDecision",
    chosen_model: str,
    checkout_delta_microusd: int = 0,
) -> dict[str, str]:
    """The ``x-sc-saar-*`` response headers for one turn (Fable SAAR design §5).
    Money-neutral, observational: they let a client correlate its own turn with
    the server-side decision-log claim and the ledger via ``replay_id``. Emitted
    whenever SAAR ran (flag on + a session), including cold/reset/drift turns, so
    the audit trail is complete; a flag-off response carries none of them.

    ``x-sc-saar-switch`` is computed from the ACTUAL committed model vs the
    session's previous model (passed by the caller post-settle), not from an
    a-priori flag — so it never claims "stayed" on a turn that actually switched
    (Fable review-1 M2). ``locked`` marks a hard lock (tool-loop or
    provider-state)."""
    if decision.reason in ("tool-loop-lock", "provider-state-lock"):
        switch = "locked"
    elif decision.prev_model and chosen_model != decision.prev_model:
        switch = "switched"
    elif decision.prev_model:
        switch = "stayed"
    else:
        switch = "none"  # cold / reset / drift — no previous model to stay on
    return {
        "x-sc-saar-replay-id": replay_id,
        "x-sc-saar-model": chosen_model,
        "x-sc-saar-phase": decision.phase,
        "x-sc-saar-switch": f"{switch}:{decision.reason}",
        "x-sc-saar-cache-tokens": str(int(decision.warm_prefix_tokens)),
        "x-sc-saar-delta-microusd": str(int(checkout_delta_microusd)),
    }


def request_has_tool_result(messages: object) -> bool:
    """True iff the CURRENT turn is returning a tool's output — i.e. the LAST
    user message carries a ``tool_result`` block. This is the tool-loop lock's
    trigger.

    Scanning only the last user message (not all of history) is essential
    correctness (Fable SAAR review-1 H2): Bedrock Converse is stateless, so a
    client re-sends the whole transcript each turn. If we scanned all messages, a
    session that opened a tool loop earlier but has since moved on to a plain
    question would STILL match the historical tool_result and stay wrongly locked,
    forbidding a legitimate reselection. Only the newest user turn tells us
    whether THIS request is a tool return. Defensive against the forward-compatible
    ``content: Any`` shape (a string / non-list ⇒ no tool result), AND against the
    message being either a plain dict OR a pydantic model — the handler passes
    ``body.messages`` which is a ``list[AnthropicMessage]`` (objects with .role/
    .content attributes), not dicts, so a dict-only ``.get`` would silently never
    fire the lock (SAAR live-verify finding)."""
    if not isinstance(messages, (list, tuple)):
        return False

    def _field(obj, name):
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    # Find the last user message (the current turn's input).
    last_user = None
    for m in messages:
        if _field(m, "role") == "user":
            last_user = m
    if last_user is None:
        return False
    content = _field(last_user, "content")
    if not isinstance(content, (list, tuple)):
        return False
    return any(_field(block, "type") == "tool_result" for block in content)


def response_has_tool_use(content_blocks: object) -> bool:
    """True iff a response's content carries a ``tool_use`` block — the model is
    asking to call a tool, so the NEXT turn's tool result must return here
    (persist phase=tool-loop). Tolerant of shape like ``request_has_tool_result``."""
    if not isinstance(content_blocks, (list, tuple)):
        return False
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return True
    return False


def next_phase_after_turn(
    *,
    response_had_tool_use: bool,
    request_had_tool_result: bool,
    response_emitted_provider_state: bool = False,
) -> str:
    """Compute the phase to PERSIST after this turn completes. Precedence:
      * the response minted non-portable continuation state (a Responses
        ``response_id`` the next turn can reference) ⇒ ``provider-state`` — the
        next request that references it must return to THIS backend;
      * else the model emitted a tool_use ⇒ the next turn's tool result must come
        back here ⇒ ``tool-loop``;
      * else ⇒ ``normal`` (a plain turn closes any loop).
    provider-state wins over tool-loop: continuation state is the stricter,
    non-portable binding, so if a turn both emitted a tool_use AND minted a
    referenceable response id, the harder provider-state lock is persisted."""
    if response_emitted_provider_state:
        return Phase.PROVIDER_STATE
    if response_had_tool_use:
        return Phase.TOOL_LOOP
    return Phase.NORMAL


# ---------------------------------------------------------------------------
# request-path adapters (thin: keep the handler clean)
# ---------------------------------------------------------------------------


@dataclass
class SaarContext:
    """Everything the handler needs to thread SAAR through one turn: the pin to
    feed ``resolve_model`` (or None), the decision (for headers/claim), the
    session identity (for the post-settle persist), and a fresh replay id. Built
    by :func:`saar_pre_reserve`; consumed by the handler for headers and by
    :func:`saar_post_settle`."""

    decision: SaarDecision
    replay_id: str
    tenant_id: str
    session_key: str
    workflow_run_id: str
    user_scoped: bool
    user_id: Optional[str]
    prev_switch_count: int
    prev_turn_count: int


def _new_replay_id() -> str:
    # ULID-ish: time-ordered enough for a replay marker, no external dep. Random
    # suffix keeps concurrent turns distinct. (Not security-sensitive.)
    import uuid

    return "rp_" + uuid.uuid4().hex[:20]


def saar_pre_reserve(
    *,
    ctx,
    org_id: str,
    user_id: Optional[str],
    request_messages: object,
    matched_decision: Optional[str] = None,
    previous_response_id: object = None,
    now_epoch: Optional[int] = None,
) -> Optional[SaarContext]:
    """Run the SAAR pre-pass for one turn, BEFORE the reserve. Returns None (SAAR
    is silent — the caller uses the normal cascade, no pin/preference) when SAAR
    is globally disabled, the tenant has no session context, or anything goes
    wrong (fail-open). Otherwise returns a :class:`SaarContext` carrying the
    decision: ``decision.hard_model`` (a hard pin → ``vsr_hard_model``, tool-loop
    lock only) and/or ``decision.prefer_model`` (a soft cascade-head preference).

    The SAAR_ENABLED flag is checked FIRST, before ANY work — no routing-config
    fetch, no memory read — so a flag-off deployment pays exactly zero SAAR cost
    and is byte-identical to pre-SAAR (Fable review-1 C1: the tenant-config fetch
    must not be eager-evaluated at the call site outside this guard).

    NEVER raises. NEVER touches money. The only side effect (when enabled) is one
    bounded memory read."""
    try:
        if not saar_enabled():
            return None
        session_key = ctx.session_key() if hasattr(ctx, "session_key") else ""
        if not session_key:
            return None
        now = int(now_epoch if now_epoch is not None else time.time())
        # Config fetch is INSIDE the flag guard + the try fence (C1): flag-off
        # never reaches here, and a fetch error fails open rather than 500ing.
        from .config import get_tenant_routing_config

        tenant_config = get_tenant_routing_config(org_id)
        user_scoped = bool(getattr(tenant_config, "saar_user_scoped", False))
        tenant_id = getattr(ctx, "tenant_id", "") or org_id
        mem = load_session_memory(
            tenant_id=tenant_id, session_key=session_key,
            user_scoped=user_scoped, user_id=user_id,
        )
        req_ps_id = (
            previous_response_id
            if isinstance(previous_response_id, str) and previous_response_id.strip()
            else None
        )
        decision = decide(
            mem=mem,
            now_epoch=now,
            request_has_tool_result=request_has_tool_result(request_messages),
            request_provider_state_id=req_ps_id,
            matched_decision=matched_decision,
        )
        return SaarContext(
            decision=decision,
            replay_id=_new_replay_id(),
            tenant_id=tenant_id,
            session_key=session_key,
            workflow_run_id=str(getattr(ctx, "workflow_run_id", "") or ""),
            user_scoped=user_scoped,
            user_id=user_id,
            prev_switch_count=int(mem.switch_count) if mem else 0,
            prev_turn_count=int(mem.turn_count) if mem else 0,
        )
    except Exception as e:  # noqa: BLE001 — fail-open to the normal cascade.
        try:
            logger.warning("saar_pre_reserve_failed", error=str(e))
        except Exception:
            pass
        return None


def saar_post_settle(
    *,
    sctx: "SaarContext",
    committed_model: str,
    response_had_tool_use: bool,
    request_had_tool_result: bool,
    minted_response_id: Optional[str] = None,
    warm_prefix_tokens: int = 0,
    rating_version: Optional[str] = None,
    checkout_delta_microusd: int = 0,
    pricing_key: Optional[str] = None,
    now_epoch: Optional[int] = None,
) -> None:
    """Persist the session's new routing state and fire the provable claim, AFTER
    the turn settled. Fire-and-forget throughout: never raises, never blocks,
    never touches a ledger transaction (money-neutrality invariant). ``sctx`` is
    the one returned by :func:`saar_pre_reserve` for this turn (None ⇒ no-op).

    ``minted_response_id`` is the provider continuation id THIS response actually
    produced (a non-empty Responses ``response_id``), or None. When present it
    both drives the persisted phase to ``provider-state`` AND is stored on the
    memory, so the NEXT turn hard-locks to ``committed_model`` ONLY if it echoes
    back exactly this id — never a forged or foreign id (Fable review §3)."""
    if sctx is None:
        return
    try:
        import time as _t

        now = int(now_epoch if now_epoch is not None else _t.time())
        prev_model = sctx.decision.prev_model
        switched = bool(prev_model) and committed_model != prev_model
        minted = (
            minted_response_id
            if isinstance(minted_response_id, str) and minted_response_id.strip()
            else None
        )
        phase = next_phase_after_turn(
            response_had_tool_use=response_had_tool_use,
            request_had_tool_result=request_had_tool_result,
            response_emitted_provider_state=minted is not None,
        )
        new_mem = SessionMemory(
            last_physical_model=committed_model,
            phase=phase,
            matched_decision=None,
            switch_count=sctx.prev_switch_count + (1 if switched else 0),
            turn_count=sctx.prev_turn_count + 1,
            last_turn_at=now,
            warm_prefix_tokens=int(warm_prefix_tokens),
            last_cache_write_at=now if warm_prefix_tokens else 0,
            rating_version=rating_version,
            replay_id=sctx.replay_id,
            minted_response_id=minted,
        )
        save_session_memory(
            tenant_id=sctx.tenant_id, session_key=sctx.session_key, mem=new_mem,
            user_scoped=sctx.user_scoped, user_id=sctx.user_id,
        )
        # Provable claim: what SAAR decided + the micro-USD checkout delta it
        # priced, pinned to rating_version so an offline reconciliation can
        # recompute it against the ledger's actual cache-read charge.
        from ..learning.decision_log import build_saar_eval_item, emit_saar_eval

        item = build_saar_eval_item(
            tenant_id=sctx.tenant_id,
            # Correlate on workflow_run_id (server-minted when the client omits
            # it, so effectively always present). Fall back to the non-sensitive
            # replay_id — NEVER the raw session_key, which build_saar_eval_item
            # would store verbatim via _safe_key_token, leaking it into the audit
            # store the session_key hashing was meant to prevent (Fable review-1 M3).
            run_id=sctx.workflow_run_id or sctx.replay_id,
            span_id=sctx.replay_id,
            session_key=sctx.session_key,
            replay_id=sctx.replay_id,
            reason=sctx.decision.reason,
            phase=phase,
            prev_model=prev_model,
            chosen_model=committed_model,
            switched=switched,
            warm_prefix_tokens_claimed=int(warm_prefix_tokens),
            checkout_delta_microusd=int(checkout_delta_microusd),
            rating_version=rating_version,
            created_at_ms=now * 1000,
        )
        emit_saar_eval(item)
    except Exception as e:  # noqa: BLE001 — post-settle is best-effort.
        try:
            logger.warning("saar_post_settle_failed", error=str(e))
        except Exception:
            pass
