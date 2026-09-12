"""E8 — the probe's ledger: a system tenant per deployment, through the
production metering path, with a cap, a rate limit and an alert.

**Who pays, and why through this path rather than a side one.** The probe
spends real money against a real model, so it bills a system tenant — never
the granting tenant, whose request the probe is not, and never nobody, which
would put an unmetered hole in a gateway whose whole point is that nothing is
unmetered. "Through the production metering path" is not a nice-to-have: it
is what makes the probe's fourth assertion (the charge resolves through the
entry's own pricing key, not `default`) mean anything, because it is the SAME
`reserve_credit` / `Hold` / `settle_reservation_and_log` machinery every real
request goes through, not a parallel bookkeeping side-channel that could
disagree with it.

`SYSTEM_TENANT_ID = "SYSTEM"` already exists at `mvp.discovery.records:65`.
Reused verbatim (imported from there), never re-minted here.

**Because the probe goes through the production reserve/settle path, the
system tenant is a tenant to every mechanism that counts money** — the
metering fault E10 added, and the eligibility axis the previous change
closed. Two consequences this module states rather than lets fall out of
default behaviour:

1. `check_probe_rate_limit` and the identity provisioned by
   `ensure_system_tenant` give the system tenant a cap and a rate limit,
   exactly as D5 requires, using the SAME primitives every other identity in
   this system is bounded by (a per-user token credit ceiling and the
   existing DynamoDB-backed fixed-window limiter) rather than inventing a
   second accounting mechanism for one caller.
2. `check_probe_scope_eligibility` states the system tenant's own
   `profile_scope` restriction EXPLICITLY, deliberately, rather than by
   silence — see that function's docstring for the choice and what it costs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from botocore.exceptions import ClientError
from fastapi import HTTPException

from core.logging import get_logger
from core.rate_limit_ddb import RateLimitExceeded, _check as _rate_limit_check, _parse_spec

from .. import _money
from .._pipeline import release_pool, reserve_credit, settle_reservation_and_log
from ..deps import AuthenticatedUser
from ..routing import config as _routing_config
from .records import SYSTEM_TENANT_ID

logger = get_logger(__name__)

#: The per-user token credit ceiling the SYSTEM identity is provisioned with —
#: a LIFETIME cap (this store has no period field; see `dynamo.user_tenants`),
#: denominated in TOKENS because that is the unit the pre-existing per-user
#: credit mechanism this reuses is denominated in, not micro-USD. Stated
#: explicitly because D5 says "a budget cap" and a reader could otherwise
#: assume a dollar figure. Reusing the existing per-user ceiling rather than
#: standing up a dollar-denominated tenant pool (with its seat-tracking and
#: period-rollover machinery, built for paying, seated customers) is the
#: right-sized "new component": one identity, one existing ceiling mechanism,
#: no new accounting concept.
SYSTEM_TENANT_TOKEN_CAP = int(
    os.getenv("STRATOCLAVE_DISCOVERY_PROBE_TOKEN_CAP", "2000000")
)

#: How many probe attempts the system tenant may make per window, independent
#: of the token cap above — the cap alone bounds total spend but not the
#: BURST rate, which matters both for provider-side throttling courtesy and
#: for bounding how fast an operator error (a loop calling the probe) can
#: spend before the cap even has a chance to bind. Parsed once at import,
#: the same convention `core.rate_limit_ddb._parse_spec` documents for its
#: own env-sourced specs: a typo fails the deploy loudly rather than silently
#: widening the window.
PROBE_RATE_LIMIT_SPEC = os.getenv("STRATOCLAVE_DISCOVERY_PROBE_RATE_LIMIT", "20/hour")
_PROBE_RATE_LIMIT, _PROBE_RATE_WINDOW_SECONDS = _parse_spec(PROBE_RATE_LIMIT_SPEC)

#: The rate limiter's scope name — `core.rate_limit_ddb`'s bucket key is
#: `RL#{scope}#{client_key}#{window}`; this module's client key is always the
#: system tenant itself (there is only ever one), so the scope alone
#: disambiguates this bucket from the auth endpoints' own buckets on the same
#: table.
_PROBE_RATE_LIMIT_SCOPE = "discovery_probe"

#: The fraction of the token cap at which `_maybe_alert` emits its
#: distinctly-named log line — the convention this repository already uses
#: for "impossible to miss in CloudWatch alerts" (see `mvp.admin_users`,
#: `mvp.sr.observability`) rather than a dedicated alerting service, which
#: this unit's scope does not include standing up.
_ALERT_THRESHOLD_FRACTION = 0.9


class ProbeAttemptRefused(ValueError):
    """The probe could not even be attempted. Raised, never returned, because
    unlike a failed assertion (`probe.ProbeResult` with `passed=False`) no
    measurement occurred at all — there is nothing for a `ProbeResult` to
    describe.

    `reason` is closed to exactly these two strings (cross-unit shape 3):

    - `probe_refused` — a DELIBERATE policy said no before any assertion ran:
      the system tenant's own scope restriction (`check_probe_scope_
      eligibility`) or its rate limit (`check_probe_rate_limit`).
    - `probe_unmetered` — the metering path itself could not be established:
      the token cap is exhausted, the identity/store could not be read, or
      (see `open_probe_hold`) the reservation failed for any other reason.
      Named for what it protects against: a probe that ran without a
      metered charge behind it is exactly the unmetered hole this contract
      refuses, so any failure to establish metering refuses the attempt
      rather than letting it proceed unbilled.
    """

    REASONS = frozenset({"probe_refused", "probe_unmetered"})

    def __init__(self, reason: str, detail: str) -> None:
        if reason not in self.REASONS:
            raise ValueError(
                f"unknown ProbeAttemptRefused reason {reason!r}; must be one "
                f"of {sorted(self.REASONS)}"
            )
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def _system_identity() -> AuthenticatedUser:
    """The synthetic identity every probe reservation is made under. Built
    fresh on every call rather than cached: it carries no state of its own
    (the state lives in the UserTenants row `ensure_system_tenant` writes),
    and constructing it is cheaper than reasoning about whether a cached
    object could go stale."""
    return AuthenticatedUser(
        user_id=SYSTEM_TENANT_ID,
        email="system-discovery-probe@stratoclave.internal",
        org_id=SYSTEM_TENANT_ID,
        roles=["admin"],
    )


def ensure_system_tenant() -> None:
    """Idempotent: provision the SYSTEM identity's per-user token credit
    ceiling, and a matching `Tenants` row, if they do not already exist. Safe
    to call before every probe attempt (`UserTenantsRepository.ensure` is
    create-if-missing, return-as-is-if-active — seen in full in `dynamo.
    user_tenants`), so a caller never has to know whether this is the first
    probe this deployment has run.

    Deliberately does NOT write a tenant routing-config row. Absence of one
    resolves to `RoutingConfig()`'s default, `profile_scopes=None` —
    unrestricted — which is `check_probe_scope_eligibility`'s DEFAULT
    posture, chosen and explained there. Writing an explicit "unrestricted"
    row here would say the same thing more expensively and would be the
    first row a later, deliberate narrowing would have to overwrite rather
    than simply create.

    **The `Tenants` row, added after checking rather than assumed away.** A
    first version of this function provisioned only the `UserTenants` row,
    reasoning that `UserTenantsRepository.ensure` only consults `Tenants`
    when `total_credit` is omitted. That is true, but it missed a SECOND
    consumer: `mvp.admin_routing.put_tenant_routing` — the existing surface
    this module's own docstring for `check_probe_scope_eligibility` points an
    operator at for a later, deliberate narrowing — calls `_require_tenant`,
    which 404s unless a `Tenants` row for the target id exists. Without one,
    the documented narrowing path would have failed the first time anyone
    tried it. `team_lead_user_id=ADMIN_OWNED` exempts it from the per-team-
    lead tenant cap (a system identity is not owned by any team lead), and a
    `ConditionalCheckFailedException` on the second and later calls — the
    row already exists — is swallowed, matching this function's own
    idempotent contract.
    """
    from botocore.exceptions import ClientError as _ClientError

    from dynamo.tenants import ADMIN_OWNED, TenantsRepository
    from dynamo.user_tenants import UserTenantsRepository

    try:
        TenantsRepository().create(
            name="Model discovery probe (system identity)",
            team_lead_user_id=ADMIN_OWNED,
            default_credit=SYSTEM_TENANT_TOKEN_CAP,
            created_by=SYSTEM_TENANT_ID,
            tenant_id=SYSTEM_TENANT_ID,
        )
    except ValueError:
        pass  # already exists — see `TenantsRepository.create`'s own contract.
    except _ClientError:
        # A read-modify-write race on first provisioning; the row exists
        # either way once this returns, which is all this function promises.
        if TenantsRepository().get_including_archived(SYSTEM_TENANT_ID) is None:
            raise

    UserTenantsRepository().ensure(
        user_id=SYSTEM_TENANT_ID,
        tenant_id=SYSTEM_TENANT_ID,
        role="admin",
        total_credit=SYSTEM_TENANT_TOKEN_CAP,
    )


def check_probe_rate_limit() -> None:
    """Raise `ProbeAttemptRefused(reason="probe_refused")` when the system
    tenant has attempted more probes than `PROBE_RATE_LIMIT_SPEC` allows in
    the current window.

    Reuses `core.rate_limit_ddb`'s existing atomic DynamoDB fixed-window
    primitive rather than building a second rate limiter: that module's own
    docstring states the failure policy (partition throttle and misconfig
    fail closed; a transient outage degrades to an in-process fallback
    rather than failing open) and reproducing it for one more caller would
    be a second, and possibly diverging, copy of that policy. The module's
    own decorator facade (`DynamoRateLimiter.limit`) requires a FastAPI
    `Request` to derive a client key from, which the probe — not an HTTP
    handler — does not have; this calls the lower-level `_check` the
    decorator itself calls, with a fixed client key (there is exactly one
    system tenant, so nothing varies the key call to call).
    """
    try:
        _rate_limit_check(
            _PROBE_RATE_LIMIT_SCOPE, SYSTEM_TENANT_ID,
            _PROBE_RATE_LIMIT, _PROBE_RATE_WINDOW_SECONDS,
        )
    except RateLimitExceeded as exc:
        raise ProbeAttemptRefused(
            "probe_refused",
            f"system tenant probe rate limit ({PROBE_RATE_LIMIT_SPEC}) exceeded",
        ) from exc


def check_probe_scope_eligibility(record: "Any") -> None:
    """Raise `ProbeAttemptRefused(reason="probe_refused")` when the system
    tenant's own configured `profile_scopes` excludes `record.profile_scope`.

    **The decision, stated because seam S3 requires it be deliberate rather
    than silent: unrestricted by default.** `ensure_system_tenant` writes no
    routing-config row, so `get_tenant_routing_config(SYSTEM_TENANT_ID)`
    resolves to `RoutingConfig()`'s default, `profile_scopes=None`, and
    `effective_profile_scopes` treats `None` as the identity element of the
    intersection — unrestricted. A probe against a `global` entry is
    therefore always reachable from this identity out of the box.

    **What that costs, stated because it is real and not merely a checked
    box.** An unrestricted system tenant can probe any entry discovery found
    in this account, including a `global` one that an ordinary tenant in a
    deployment configured to restrict itself to `us` could never reach
    through the normal eligibility predicate (`mvp.eligibility.refusal_for`).
    That is defensible for exactly what this identity is: a synthetic,
    operator-initiated call whose purpose IS to verify the binding for
    whichever scope discovery observed, on behalf of the deployment as a
    whole rather than any one tenant's residency posture. It would NOT be
    defensible if this identity, or its unrestricted scope, were ever reused
    for anything else — a route that let an ordinary caller ride this
    identity's reservation, for instance, would reintroduce exactly the
    bypass the previous change closed. Nothing in this unit does that; it is
    stated here so a future change touching this identity checks it again
    rather than assuming the unrestricted posture is free.

    The check itself deliberately reuses only the SCOPE axis of `mvp.
    eligibility.refusal_for`'s three, not the whole predicate: that
    predicate takes a `ModelEntry`, which does not exist for a record that
    has not been promoted and activated yet (this IS pre-activation
    verification) — there is no model-policy allowlist axis or entitlement
    axis to check against something that is not yet a registry entry. Only
    the scope axis has a subject before activation: the record's own
    `profile_scope`, compared against a tenant-level restriction that exists
    independently of any one model.

    Live, not cached at provisioning time: an operator MAY narrow the system
    tenant's `profile_scopes` later, through the existing tenant-routing
    admin surface keyed on `SYSTEM_TENANT_ID` — no new surface is built for
    this — and this function re-reads the current configuration on every
    call, so that narrowing takes effect on the very next probe rather than
    requiring a restart or a re-provision.
    """
    # Module-qualified, not `from ... import ...`-bound at load time: a test
    # (or an operator's own monkeypatch) that replaces `mvp.routing.config.
    # get_tenant_routing_config` after this module has already imported must
    # still be honoured, and a name bound once at import time cannot see a
    # later reassignment of the module attribute it was copied from.
    tenant_cfg = _routing_config.get_tenant_routing_config(SYSTEM_TENANT_ID)
    scopes = _routing_config.effective_profile_scopes(tenant_cfg, None)
    if scopes is not None and record.profile_scope not in scopes:
        raise ProbeAttemptRefused(
            "probe_refused",
            f"system tenant is restricted to profile_scopes={sorted(scopes)!r}; "
            f"record profile_scope={record.profile_scope!r} is outside it",
        )


def _maybe_alert(remaining_tokens: int) -> None:
    """Emit the distinctly-named log line an operator's CloudWatch alarm is
    expected to key on, once the system tenant's remaining token headroom
    crosses below `_ALERT_THRESHOLD_FRACTION` of its cap. Deliberately a log
    line rather than a new notification channel: every other "alert" in this
    repository (see `mvp.admin_users`, `mvp.sr.observability`) is a
    distinguishable structured log an operator wires a metric filter to, not
    a bespoke paging integration this unit's scope does not include.
    """
    threshold = int(SYSTEM_TENANT_TOKEN_CAP * (1 - _ALERT_THRESHOLD_FRACTION))
    if remaining_tokens <= threshold:
        logger.warning(
            "discovery_probe_ledger_cap_nearly_exhausted",
            remaining_tokens=remaining_tokens,
            cap_tokens=SYSTEM_TENANT_TOKEN_CAP,
            threshold_tokens=threshold,
        )


def _open_hold(**kwargs) -> _money.Hold:
    """Mirrors every route module's own `_open_hold` exactly (see
    `mvp.anthropic._open_hold`): `settle`/`release` resolve the real
    production functions at call time so this probe's charge lands through
    the identical write path a real request's does. No `mark_departed`
    writer — the probe never runs unattended long enough for the reaper's
    retention story to apply, and it has no dollar-pool hold to retain
    against (see the module docstring: the cap is a per-user token ceiling,
    not a pool)."""
    return _money.Hold(
        settle=lambda **kw: settle_reservation_and_log(**kw),
        release=lambda ctx: release_pool(ctx),
        **kwargs,
    )


def open_probe_hold(
    *, pricing_key: str, model_id: str, invocation: str,
    input_tokens_est: int, max_output_tokens: int,
) -> _money.Hold:
    """Reserve credit for one probe attempt against the SYSTEM tenant through
    the production `reserve_credit` chokepoint (not `reserve_credit_for_
    model`: that wrapper prices VSR/task-tag/hard-ceiling-byte-survey
    machinery meant for an inbound HTTP request, none of which a synthetic
    probe has or needs — `reserve_credit` is the primitive underneath it that
    both `reserve_credit_for_model` and this call reduce to), and return the
    `Hold` that owns ending it.

    Every failure to reserve becomes `ProbeAttemptRefused(reason=
    "probe_unmetered")`: the token cap exhausted, the identity unprovisioned
    (should not happen after `ensure_system_tenant`, guarded anyway), or the
    per-user store unreachable are all, from the probe's point of view, the
    same fact — metering could not be established for this attempt, so the
    attempt must not proceed unbilled.
    """
    from ..pricing import estimate_cost_from_rates, snapshot_rates

    try:
        rate_snapshot = snapshot_rates(pricing_key)
    except Exception as exc:  # noqa: BLE001 — one refusal, stated once.
        raise ProbeAttemptRefused(
            "probe_unmetered", f"pricing snapshot unavailable for {pricing_key!r}: {exc}"
        ) from exc
    cost_microusd = estimate_cost_from_rates(
        rate_snapshot, input_tokens_est=input_tokens_est, max_output_tokens=max_output_tokens,
    )
    reservation_tokens = int(input_tokens_est) + int(max_output_tokens)
    user = _system_identity()
    try:
        context = reserve_credit(
            user, reservation_tokens,
            pricing_key=pricing_key, cost_microusd=cost_microusd,
            selected_model=model_id, rate_snapshot=rate_snapshot,
        )
    except HTTPException as exc:
        raise ProbeAttemptRefused(
            "probe_unmetered",
            f"reserve_credit refused the probe reservation: {exc.detail!r}",
        ) from exc
    from dynamo.user_tenants import UserTenantsRepository

    remaining = UserTenantsRepository().remaining_credit(SYSTEM_TENANT_ID, SYSTEM_TENANT_ID)
    _maybe_alert(remaining)
    return _open_hold(
        user=user, tenants_repo=context, reservation=reservation_tokens,
        model_id=model_id, route=f"discovery_probe_{invocation}",
    )
