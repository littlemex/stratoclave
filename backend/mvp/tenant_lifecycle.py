"""Creating a tenant: the one operation both creation routes call (#243, H1). The
rest of a tenant's lifecycle is meant to land beside it.

`POST /api/mvp/admin/tenants` and `POST /api/mvp/team-lead/tenants` differ in who the
owner is and how that is authorised: the admin route verifies an owner named in the
body, the team-lead route pins the owner to the caller. Everything else about
creating a tenant is the same for both, and lives here: the seat-rate check, the
tenant row and its limit, the audit record, and the per-tenant provisioning steps.

Each provisioning step declares when it runs and what its failure means
(`StepPolicy`), because the steps disagree:

- `REFUSES_BEFORE_ROW` runs before the tenant row is written, so its failure refuses
  the creation and leaves no tenant row. None exists yet; a step that must be able to
  refuse creation (a per-tenant inference profile, #239) goes here. The tenant limit
  is checked before these steps run. Nothing tells a step that the row write after it
  failed, so whatever it created is left behind; the step that needs that undone
  brings the undo with it.
- `REQUIRED_AFTER_ROW` runs after the row. Its failure fails the request, and the
  tenant row remains. The seat-tracked pool is one. Its residual is not refusing:
  a tenant row whose pool write failed has no dollar pool ceiling (C14.1, C14.36).
- `BEST_EFFORT` runs after the row, and its failure is logged and does not fail the
  request. The shadow routing default is one.

The audit record is written right after the row and before the after-row steps, so
the log reads create, then provision. Writing it can fail only if the log handler
raises; that fails the request with the tenant row in place, the same residual a
`REQUIRED_AFTER_ROW` step has.

**Tenants outside this lifecycle.** Two tenants are written by bootstrap code rather
than by a creation route, and neither goes through here:

- `DEFAULT_ORG_ID` (`bootstrap.seed.seed_default_tenant`, through
  `TenantsRepository.seed_default`): the fallback organisation new users land in.
- The model-discovery system tenant (`mvp.discovery.ledger.ensure_system_tenant`):
  the identity probes are charged to.

Neither gets a seat-tracked pool or a shadow routing default, and neither emits a
creation audit record. They are deployment fixtures with no owner to admit, and the
discovery tenant deliberately has no routing-config row (see its docstring).
`test_tenant_lifecycle.py` pins that these are the only other writers of a tenant
row.
"""
from __future__ import annotations

import enum
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

from dynamo import TenantBudgetsRepository, TenantsRepository, current_period
from dynamo.tenant_budgets import seat_pool_limit_microusd

from core.logging import get_logger

from .authz import log_audit_event
from .deps import AuthenticatedUser

_log = get_logger(__name__)


class StepPolicy(enum.Enum):
    REFUSES_BEFORE_ROW = "refuses_before_row"
    REQUIRED_AFTER_ROW = "required_after_row"
    BEST_EFFORT = "best_effort"


@dataclass(frozen=True)
class NewTenant:
    """What a provisioning step is given. The tenant id is fixed before any step
    runs, so a step before the row can key on it."""

    tenant_id: str
    actor: AuthenticatedUser


@dataclass(frozen=True)
class ProvisioningStep:
    name: str
    policy: StepPolicy
    run: Callable[[NewTenant], None]


def _provision_seat_pool(new: NewTenant) -> None:
    """The tenant's default dollar pool for the current period: seat-tracked, at
    ZERO seats. It reaches `seats x rate` through the same one-seat delta every
    membership change applies, so the ceiling equals the seat count at every moment.
    Zero rather than one, because a fresh tenant has no memberships yet and the
    first `ensure` adds its own seat."""
    period = current_period()
    TenantBudgetsRepository().create_seat_tracked_pool(
        tenant_id=new.tenant_id, period=period, seat_count=0)
    log_audit_event(
        event="tenant_pool_provisioned",
        actor_id=new.actor.user_id,
        actor_email=new.actor.email,
        target_id=new.tenant_id,
        target_type="tenant",
        after={"period": period, "pool_limit_microusd": seat_pool_limit_microusd(0),
               "seat_tracked": True, "seats": 0},
    )


def shadow_default_enabled() -> bool:
    """`STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT` opts every new tenant out; the
    common falsy spellings all count."""
    return os.getenv("STRATOCLAVE_SHADOW_VSR_NEW_TENANT_DEFAULT", "true").strip().lower() not in (
        "false", "0", "no", "off")


def _provision_shadow_default(new: NewTenant) -> None:
    """An explicit `shadow_vsr=True` routing-config record, so the Savings Certificate
    is populated from the first week and the state is visible rather than implied."""
    if not shadow_default_enabled():
        return
    from . import admin_routing

    admin_routing.provision_shadow_default_config(new.tenant_id, updated_by=new.actor.user_id)


STEPS: tuple[ProvisioningStep, ...] = (
    ProvisioningStep("seat_pool", StepPolicy.REQUIRED_AFTER_ROW, _provision_seat_pool),
    ProvisioningStep("shadow_default", StepPolicy.BEST_EFFORT, _provision_shadow_default),
)


def create_tenant(
    *,
    name: str,
    team_lead_user_id: str,
    default_credit: Optional[int],
    actor: AuthenticatedUser,
    audit_event: str,
    audit_details: dict[str, Any],
) -> dict[str, Any]:
    """Create a tenant owned by `team_lead_user_id`, which the caller has already
    resolved and authorised. Returns the tenant row. The steps are `STEPS`.

    Raises `PoolLimitExceedsMaximumError` before anything is written when one seat at
    the configured rate exceeds the maximum pool, `TenantLimitExceededError` when the
    owner holds the most tenants allowed, and whatever a `REFUSES_BEFORE_ROW` or
    `REQUIRED_AFTER_ROW` step raises. The routes map these to their responses."""
    steps = STEPS
    tenants = TenantsRepository()
    seat_pool_limit_microusd(1)
    # Refused before any step runs, so a step before the row is not done for an
    # owner the row write would refuse. `create` checks again at the write.
    tenants.ensure_below_tenant_limit(team_lead_user_id)
    new = NewTenant(tenant_id=tenants.new_tenant_id(), actor=actor)

    for step in steps:
        if step.policy is StepPolicy.REFUSES_BEFORE_ROW:
            step.run(new)

    item = tenants.create(
        tenant_id=new.tenant_id,
        name=name,
        team_lead_user_id=team_lead_user_id,
        default_credit=default_credit,
        created_by=actor.user_id,
    )
    log_audit_event(
        event=audit_event,
        actor_id=actor.user_id,
        actor_email=actor.email,
        target_id=new.tenant_id,
        target_type="tenant",
        details=audit_details,
    )

    for step in steps:
        if step.policy is StepPolicy.REQUIRED_AFTER_ROW:
            step.run(new)
        elif step.policy is StepPolicy.BEST_EFFORT:
            try:
                step.run(new)
            except Exception as e:  # noqa: BLE001 -- best effort by declaration
                _log.warning("tenant_provisioning_step_failed", step=step.name,
                             tenant_id=new.tenant_id, error=str(e))
    return item
