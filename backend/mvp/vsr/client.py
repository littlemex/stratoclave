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


# Consult outcomes observable AT CONSULT TIME (before the resolver runs). This
# is the gateway-side half of the VSR observability boundary (Fable design): we
# record what the VSR advised and why we did/didn't get advice — NOT the routing
# quality (that is the VSR's own Prometheus/Grafana stack). The post-reserve
# enforcement outcomes (rejected-by-allowlist / prefer-overridden) are a later
# increment and are NOT decided here.
CONSULT_FLAG_OFF = "flag-off"        # feature disabled — no consult attempted
CONSULT_UNVERIFIED = "unverified"    # handshake not VERIFIED — consult skipped
CONSULT_TIMEOUT = "timeout"          # consult attempted, transport failed/slow
CONSULT_NO_ADVICE = "no-advice"      # consulted OK but VSR returned no usable pin
CONSULT_SUGGESTED = "suggested"      # VSR returned a usable suggestion (see mode)


@dataclass(frozen=True)
class VsrConsultResult:
    """The outcome of one consult, for observability. `suggestion` is non-None
    ONLY when `outcome == CONSULT_SUGGESTED`. This never carries the VSR URL or
    the tenant config contents — only the advised model id (already destined for
    the same allowlist enforcement as a client pin).

    `config_version` is the opaque effective-config id the RUNNING VSR echoed on
    the consult response (contract: header `x-vsr-config-version`), or None if it
    did not echo one. Compared against the S3 version Stratoclave wrote at PUT,
    this is how config validate/serve SKEW is detected — the running VSR may
    lazy-load an older blob or fall back to last-known-good/default silently, and
    only the writer (Stratoclave) can notice its write is not the one in effect."""

    outcome: str
    suggestion: Optional[VsrSuggestion] = None
    config_version: Optional[str] = None


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


def consult_ex(*, tenant_id: str, session_key: Optional[str],
               requested_model: str) -> VsrConsultResult:
    """Ask the VSR for a routing suggestion and report the OUTCOME (for
    observability), not just the suggestion. Returns a VsrConsultResult whose
    `suggestion` is non-None only when `outcome == CONSULT_SUGGESTED`.

    Trust/fail-open semantics are unchanged from `consult()`: a suggestion is
    honored only when the flag is on AND the pin state is VERIFIED AND the
    consult succeeds within budget AND the response carries the pinned contract
    header AND parses cleanly. Any deviation yields NO suggestion — but now the
    reason is surfaced (flag-off / unverified / timeout / no-advice) so the
    caller can emit a single honest decision log line without guessing."""
    if not external_vsr_enabled():
        return VsrConsultResult(CONSULT_FLAG_OFF)
    if get_state() != VERIFIED:
        return VsrConsultResult(CONSULT_UNVERIFIED)
    client = _get_client()
    if client is None:
        # Base url unset while flag on: treat as unverified (no peer to consult).
        return VsrConsultResult(CONSULT_UNVERIFIED)
    try:
        resp = client.post(
            "/v1/route",
            json={"tenant_id": tenant_id, "session_key": session_key,
                  "requested_model": requested_model},
        )
    except Exception as e:  # noqa: BLE001 — fail-open, hot-path budget honored.
        logger.info("vsr_consult_failed", error=str(e))
        return VsrConsultResult(CONSULT_TIMEOUT)
    # Belt-and-suspenders: a mid-flight VSR redeploy inside the handshake
    # interval is caught by the per-response contract header. On mismatch,
    # discard this response AND flip to REFUSED pending re-handshake.
    if resp.headers.get("x-vsr-contract") != _expected_contract():
        _set_state(REFUSED)
        logger.warning("vsr_consult_contract_mismatch")
        return VsrConsultResult(CONSULT_NO_ADVICE)
    # The effective-config id the running VSR served this consult with (contract
    # addition). Optional: an older VSR simply omits it -> None -> skew undetected
    # (never an error). Bounded to a sane length so a hostile echo can't bloat the
    # decision record.
    cfg_ver = resp.headers.get("x-vsr-config-version")
    if isinstance(cfg_ver, str):
        cfg_ver = cfg_ver.strip()[:128] or None
    else:
        cfg_ver = None
    if resp.status_code != 200:
        return VsrConsultResult(CONSULT_NO_ADVICE, config_version=cfg_ver)
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return VsrConsultResult(CONSULT_NO_ADVICE, config_version=cfg_ver)
    model = body.get("pin_model")
    mode = body.get("mode")
    if not isinstance(model, str) or not model or mode not in ("hard", "prefer"):
        return VsrConsultResult(CONSULT_NO_ADVICE, config_version=cfg_ver)
    return VsrConsultResult(
        CONSULT_SUGGESTED, VsrSuggestion(model=model, mode=mode), config_version=cfg_ver)


def consult(*, tenant_id: str, session_key: Optional[str],
            requested_model: str) -> Optional[VsrSuggestion]:
    """Backward-compatible thin wrapper over `consult_ex`: returns just the
    suggestion (or None). New callers that want the outcome for observability
    should call `consult_ex` directly."""
    return consult_ex(tenant_id=tenant_id, session_key=session_key,
                      requested_model=requested_model).suggestion


# Final per-request decision labels the caller logs. These REFINE a
# CONSULT_SUGGESTED outcome by what the caller did with the advice; the
# non-suggested outcomes (flag-off/unverified/timeout/no-advice) pass through
# unchanged. The post-reserve enforcement split (rejected-by-allowlist) is a
# later increment and is intentionally NOT decided here.
DECISION_HARD_APPLIED = "hard-applied"
DECISION_PREFER_APPLIED = "prefer-applied"
DECISION_PREFER_OVERRIDDEN = "prefer-overridden"  # local SAAR prefer already won
# The judgment-only shadow decision (litellm drop-in wedge, docs/design/
# vsr-savings-certificate.md): a (local, rule-based) shadow VSR advised a cheaper
# model but execution was NOT steered — the client pin was billed. It is NOT a
# steering decision (nothing was enacted); it feeds the SEPARATE "potential"
# savings base, never mixed into realized savings.
DECISION_SHADOW_ADVISED = "shadow-advised"

# The decisions in which the VSR's suggestion ACTUALLY STEERED the model this
# turn — the base for REALIZED "if you'd followed the VSR" savings
# (mvp.learning.savings). SINGLE SOURCE OF TRUTH for the REALIZED base: savings
# imports this so a new steering label added here can never silently shrink it
# (Fable review finding d). PREFER_OVERRIDDEN is EXCLUDED — a local SAAR prefer
# held the head, so the VSR's prefer did not take effect. SHADOW_ADVISED is
# EXCLUDED on purpose: it did not steer, so putting it here would be a naming lie
# ("STEERING" must mean execution was intervened) — it has its own base below.
STEERING_DECISIONS = frozenset({DECISION_HARD_APPLIED, DECISION_PREFER_APPLIED})

# The judgment-only base: advice given, execution NOT enacted. SINGLE SOURCE OF
# TRUTH for the POTENTIAL savings base (Fable shadow-label review: two separate
# bases, never a union — potential must never be summed into realized).
SHADOW_DECISIONS = frozenset({DECISION_SHADOW_ADVISED})


def classify_consult_decision(result: "VsrConsultResult", *,
                              saar_prefer_present: bool) -> str:
    """PURE mapping from a consult result to the decision label to log.

    * a hard suggestion  -> DECISION_HARD_APPLIED (it becomes the enforced pin);
    * a prefer suggestion -> DECISION_PREFER_APPLIED, unless a local SAAR prefer
      already holds the cascade head, in which case the VSR prefer does not take
      effect this turn -> DECISION_PREFER_OVERRIDDEN;
    * anything else (flag-off / unverified / timeout / no-advice) -> the raw
      consult outcome, verbatim.

    Note this reflects only whether the VSR advice ENTERED the resolver, not the
    final post-allowlist committed model (a later increment)."""
    s = result.suggestion
    if s is None:
        return result.outcome
    if s.mode == "hard":
        return DECISION_HARD_APPLIED
    if s.mode == "prefer":
        return DECISION_PREFER_OVERRIDDEN if saar_prefer_present else DECISION_PREFER_APPLIED
    return result.outcome  # pragma: no cover — mode is validated upstream


# Response header names, following the existing `x-sc-saar-*` convention. These
# are OBSERVATIONAL (a debugging aid so an operator/client can see, per request,
# whether the VSR was consulted and honored) — never authoritative and never
# carrying the session key / VSR url / config contents.
HDR_VSR_DECISION = "x-sc-vsr-decision"
HDR_VSR_SUGGESTED = "x-sc-vsr-suggested"
HDR_VSR_CONFIG_VERSION = "x-sc-vsr-config-version"


def decision_record(result: "VsrConsultResult", *, saar_prefer_present: bool) -> dict:
    """PURE: the VSR block to attach to the reserve-time decision log record.
    Only the fields safe to persist: the decision label, the advised model +
    mode (when suggested), and the effective-config id the VSR echoed. NO raw
    session key, NO VSR url, NO tenant config contents."""
    s = result.suggestion
    rec = {"decision": classify_consult_decision(result, saar_prefer_present=saar_prefer_present)}
    if s is not None:
        rec["suggested_model"] = s.model
        rec["mode"] = s.mode
    if result.config_version:
        rec["config_version"] = result.config_version
    return rec


def decision_headers(result: "VsrConsultResult", *, saar_prefer_present: bool) -> dict:
    """PURE: the `x-sc-vsr-*` response headers for one request, mirroring SAAR's
    replay headers. Same safety as decision_record — only the decision label, the
    advised model, and the echoed config id (all header-safe scalars)."""
    s = result.suggestion
    hdrs = {HDR_VSR_DECISION: classify_consult_decision(result, saar_prefer_present=saar_prefer_present)}
    if s is not None:
        hdrs[HDR_VSR_SUGGESTED] = s.model
    if result.config_version:
        hdrs[HDR_VSR_CONFIG_VERSION] = result.config_version
    return hdrs
