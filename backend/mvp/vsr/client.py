"""External VSR (Value/Session Router) consult client — version-pinned,
fail-open, off by default.

The VSR is an OPTIONAL, EXTERNAL, operator-run service (a separate ECS task)
that can suggest a routing pin (a model + hard/prefer mode) centrally. It is an
UNTRUSTED ADVISOR: its suggestion is fed into the SAME resolver inputs as the
`x-sc-model-pin` header (`vsr_hard_model` / `saar_prefer_model`) and therefore
passes the SAME tenant allowlist + servability enforcement. Consulting it never
grants new trust and never bypasses a budget or an allowlist.

Because it is external, it MUST be version-pinned (task #13): a drifting VSR
must not silently change routing. Enforcement is two-layered:

  1. IaC pins the VSR container by digest/exact-semver (synth-time guard).
  2. Runtime handshake (this module): before ANY consult is honored, the VSR's
     advertised contract+build must match the pinned set. On mismatch or
     unreachability we go to REFUSED / UNVERIFIED: NO consults, routing is
     exactly today's (Bedrock cascade). We NEVER follow an unknown VSR.

Everything here is INERT unless EXTERNAL_VSR_ENABLED=true. With it off, no
handshake task runs, no HTTP client is built, and `consult()` returns None
immediately — routing is byte-behaviour-identical to today.

The consult itself mirrors the SAAR memory read: short timeout, no retry,
fail-open (any error/timeout -> None -> normal resolver). A consult is NEVER on
a money-correctness path.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Optional

import httpx

from core.logging import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Flag + pinned-version config (all operator-set env / IaC params).
# --------------------------------------------------------------------------

def external_vsr_enabled() -> bool:
    return os.getenv("EXTERNAL_VSR_ENABLED", "false").lower() == "true"


def _base_url() -> str:
    return (os.getenv("VSR_BASE_URL", "") or "").rstrip("/")


def _expected_contract() -> str:
    # The pinned wire contract (e.g. "vsr/1"). Empty => nothing can match =>
    # every handshake REFUSES (fail-closed) — a safe default if unset.
    return os.getenv("VSR_EXPECTED_CONTRACT", "").strip()


def _expected_builds() -> frozenset[str]:
    raw = os.getenv("VSR_EXPECTED_BUILDS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(b.strip() for b in raw.split(",") if b.strip())


# Handshake / consult timeouts: both short. The consult budget mirrors the SAAR
# read (a hot-path advisory), the handshake is off the hot path.
_CONSULT_TIMEOUT_S = 0.15
_HANDSHAKE_TIMEOUT_S = 1.0


# --------------------------------------------------------------------------
# Version-pin state machine (process-local per ECS task).
#
# Per-task state is fine: the state gates an ADVISORY, fail-open consult, so a
# task in REFUSED simply routes as today (Bedrock cascade). A mixed fleet during
# a deploy means temporarily inconsistent *advice*, never inconsistent
# *enforcement* — the allowlist/servability checks are per-request and
# unconditional, and a REFUSED task still serves 200s (it never 500s on VSR).
# --------------------------------------------------------------------------

UNVERIFIED = "unverified"
VERIFIED = "verified"
REFUSED = "refused"

_state = UNVERIFIED
_state_lock = threading.Lock()
_client: Optional[httpx.Client] = None
_client_lock = threading.Lock()


@dataclass(frozen=True)
class VsrSuggestion:
    """A parsed, validated consult response. `mode` is "hard" (a tool-loop-style
    hard pin) or "prefer" (a soft cascade-head reorder). `model` is a
    client-facing model id that STILL passes the tenant allowlist downstream."""

    model: str
    mode: str  # "hard" | "prefer"


def get_state() -> str:
    with _state_lock:
        return _state


def _set_state(new: str) -> None:
    global _state
    with _state_lock:
        prev = _state
        _state = new
    if prev != new:
        logger.info("vsr_state_changed", previous=prev, current=new)


def reset_for_test() -> None:
    global _state, _client
    with _state_lock:
        _state = UNVERIFIED
    with _client_lock:
        if _client is not None:
            try:
                _client.close()
            except Exception:  # noqa: BLE001
                pass
        _client = None


def _get_client() -> Optional[httpx.Client]:
    global _client
    base = _base_url()
    if not base:
        return None
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                base_url=base,
                timeout=httpx.Timeout(connect=_CONSULT_TIMEOUT_S, read=_CONSULT_TIMEOUT_S,
                                      write=_CONSULT_TIMEOUT_S, pool=_CONSULT_TIMEOUT_S),
            )
        return _client


def _version_matches(payload: dict) -> bool:
    """A /version payload matches the pin iff its contract equals the pinned
    contract AND its build is in the pinned build set. An empty pinned contract
    or empty pinned build set matches NOTHING (fail-closed)."""
    contract = _expected_contract()
    builds = _expected_builds()
    if not contract or not builds:
        return False
    return payload.get("contract") == contract and payload.get("version") in builds


def handshake() -> str:
    """GET /version and update the pin state. Off the hot path (startup + a
    periodic task). Returns the new state.

      * contract/build in the pinned set        -> VERIFIED
      * reachable but contract/build mismatch    -> REFUSED (never follow it)
      * unreachable / malformed                  -> UNVERIFIED (degrade; == VSR
        absent; auto-heals on a later successful handshake)
    """
    if not external_vsr_enabled():
        _set_state(UNVERIFIED)
        return UNVERIFIED
    client = _get_client()
    if client is None:
        _set_state(UNVERIFIED)
        return UNVERIFIED
    try:
        resp = client.get("/version", timeout=_HANDSHAKE_TIMEOUT_S)
        if resp.status_code != 200:
            _set_state(REFUSED)
            logger.warning("vsr_version_refused", reason="non_200",
                           status=resp.status_code)
            return REFUSED
        payload = resp.json()
    except Exception as e:  # noqa: BLE001 — unreachable/malformed => degrade, not refuse.
        _set_state(UNVERIFIED)
        logger.info("vsr_handshake_unreachable", error=str(e))
        return UNVERIFIED
    if _version_matches(payload if isinstance(payload, dict) else {}):
        _set_state(VERIFIED)
        return VERIFIED
    _set_state(REFUSED)
    logger.warning(
        "vsr_version_refused", reason="contract_or_build_mismatch",
        got_contract=(payload.get("contract") if isinstance(payload, dict) else None),
        got_version=(payload.get("version") if isinstance(payload, dict) else None),
    )
    return REFUSED


def consult(*, tenant_id: str, session_key: Optional[str],
            requested_model: str) -> Optional[VsrSuggestion]:
    """Ask the VSR for a routing suggestion. Returns None (fall back to the
    normal resolver) UNLESS the flag is on AND the pin state is VERIFIED AND the
    consult succeeds within budget AND the response carries the pinned contract
    header AND parses cleanly. Any deviation -> None (fail-open).

    This is deliberately permissive on failure and strict on trust: a missing,
    slow, or version-skewed VSR simply yields no advice."""
    if not external_vsr_enabled():
        return None
    if get_state() != VERIFIED:
        return None
    client = _get_client()
    if client is None:
        return None
    try:
        resp = client.post(
            "/v1/route",
            json={"tenant_id": tenant_id, "session_key": session_key,
                  "requested_model": requested_model},
        )
    except Exception as e:  # noqa: BLE001 — fail-open, hot-path budget honored.
        logger.info("vsr_consult_failed", error=str(e))
        return None
    # Belt-and-suspenders: a mid-flight VSR redeploy inside the handshake
    # interval is caught by the per-response contract header. On mismatch,
    # discard this response AND flip to REFUSED pending re-handshake.
    if resp.headers.get("x-vsr-contract") != _expected_contract():
        _set_state(REFUSED)
        logger.warning("vsr_consult_contract_mismatch")
        return None
    if resp.status_code != 200:
        return None
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    model = body.get("pin_model")
    mode = body.get("mode")
    if not isinstance(model, str) or not model or mode not in ("hard", "prefer"):
        return None
    return VsrSuggestion(model=model, mode=mode)
