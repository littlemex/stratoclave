"""Tenants table (Phase 2).

Table design (iac/lib/dynamodb-stack.ts):
  PK: tenant_id (no sort key)
  GSI team-lead-index: PK team_lead_user_id, SK created_at, ProjectionType ALL
  Attributes:
    tenant_id: str
    name: str
    team_lead_user_id: str  (Cognito sub; "admin-owned" when owned by an admin)
    default_credit: int
    status: "active" | "archived"
    created_at: str (ISO 8601)
    updated_at: str (ISO 8601)
    created_by: str
    user_dollar_defaults: {period -> microusd}       (PR 3, see below)
    sealed_user_dollar_base: {period -> microusd}     (PR 3, see below)
    user_dollar_defaults_version: int                 (PR 3, see below)

PR 3's per-user money ceiling (mvp.routing.user_dollar_quota) reads its base
from THIS row rather than from a fresh one of its own. `default_credit` above
is the per-user TOKEN quota's default; `user_dollar_defaults` is money, named
differently on purpose (a name collision between the two on the same row would
make an admin fat-finger one for the other with no serialisation error to
catch it) -- see the field's own docstring below.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource


ADMIN_OWNED = "admin-owned"

# Per-tenant reservation-bound mode (docs/design/hard-ceiling.md,
# mvp/reservation_bound.py):
#   "strict"     — the sound byte-based bound; overspending the pool is
#                   impossible by construction (within the stated
#                   assumptions), at the cost of reserving several times the
#                   eventual actual spend.
#   "calibrated" — docs/design/calibrated-mode.md, deliberately NOT part of this
#                   change. The constant is defined so phase 2's code can
#                   name it, but it is NOT in `VALID_BOUND_MODES` below, so it
#                   is UNREACHABLE today: `update()` refuses to set it and
#                   `resolve_bound_mode` can never return it for a real
#                   tenant. Phase 2 adds it back to `VALID_BOUND_MODES` when
#                   its own ship gate (a real shadow-run measurement) is met.
# Absent (no attribute on the row) defaults to "strict" — see
# `dynamo.tenants.resolve_bound_mode`.
BOUND_MODE_STRICT = "strict"
BOUND_MODE_CALIBRATED = "calibrated"
VALID_BOUND_MODES = frozenset({BOUND_MODE_STRICT})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_period() -> str:
    """The current billing period key (calendar month, UTC): "2026-09".

    Deliberately NOT imported from the tenant pool's own repository module,
    which defines the identical one-liner: a write-discipline guard in the
    test suite text-matches that module's name to find every writer of the
    pool's own table and requires each to be on its reviewed allowlist. This
    module writes to a DIFFERENT table (`stratoclave-tenants`, never the
    pool's) and importing from that module would be a false positive on that
    guard rather than a real writer needing review. Duplicated here (a
    six-line pure function) rather than traded for that false positive.
    """
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def default_tenant_credit() -> int:
    """THE per-user token backstop, in tokens. One definition, read at call time.

    Two call sites need this number: `TenantsRepository.create` stamps it onto a
    new tenant's `default_credit`, and `UserTenantsRepository._resolve_tenant_default`
    falls back to it for a tenant row that carries none. They were separate
    literals (100,000 here and 100,000 there), so raising one would have left a
    membership resolved through the other at the old ceiling. Read at call time,
    not bound at import, so an operator's `DEFAULT_TENANT_CREDIT` takes effect
    without a redeploy of the reader.

    Raised from 100,000 to 10,000,000 (docs/design/limits.md, L2): the per-user
    token quota is a loose fairness backstop, not the binding ceiling — the
    tenant dollar pool is (docs/design/limits.md states which ceiling protects what). Deliberately loose
    rather than unlimited: with it removed, a single user could consume the
    whole tenant pool with no fairness device to replace it. Raising this
    default changes no admission arithmetic; it is still the same per-user
    token item, at a bigger number.
    """
    return int(os.getenv("DEFAULT_TENANT_CREDIT", "10000000"))


def _default_credit_fallback() -> int:
    """Historical name kept for existing call sites; see `default_tenant_credit`."""
    return default_tenant_credit()


def _tenants_table_name() -> str:
    return os.getenv("DYNAMODB_TENANTS_TABLE", "stratoclave-tenants")


class TenantNotFoundError(Exception):
    """Raised when the requested tenant does not exist."""


class TenantLimitExceededError(Exception):
    """Raised when a team lead exceeds the tenant creation limit."""


class TenantsRepository:
    """CRUD operations for the Tenants table.

    The team lead limit of 50 tenants (v2.1 §4.4) is enforced in `create`.
    """

    TEAM_LEAD_TENANT_LIMIT = 50

    def __init__(self, table_name: Optional[str] = None) -> None:
        self._table = get_dynamodb_resource().Table(
            table_name or _tenants_table_name()
        )

    # ----- read -----
    def get(
        self, tenant_id: str, *, consistent_read: bool = False
    ) -> Optional[dict[str, Any]]:
        """`consistent_read=True` is what the seal (P3.3) needs: it CASes on
        `user_dollar_defaults_version`, and a stale eventually-consistent read
        can carry a version that has already moved, making the CAS fail
        forever on a tenant that is not actually contended. Every other caller
        keeps the cheaper eventually-consistent default (unchanged)."""
        resp = self._table.get_item(
            Key={"tenant_id": tenant_id}, ConsistentRead=consistent_read
        )
        item = resp.get("Item")
        if item and item.get("status") == "archived":
            return None
        return item

    def get_including_archived(self, tenant_id: str) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(Key={"tenant_id": tenant_id})
        return resp.get("Item")

    def list_by_owner(self, owner_user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        resp = self._table.query(
            IndexName="team-lead-index",
            KeyConditionExpression=Key("team_lead_user_id").eq(owner_user_id),
            Limit=min(limit, 100),
        )
        return [item for item in resp.get("Items", []) if item.get("status") != "archived"]

    def count_by_owner(self, owner_user_id: str) -> int:
        """Count *active* tenants owned by `owner_user_id`.

        A-04-tenant: archived tenants must NOT count toward the team-lead
        cap. Otherwise a team lead who archives a tenant cannot create a
        new one even though their visible footprint is below the limit,
        and the cap silently inflates over time as archives accumulate.

        Implementation note: DynamoDB COUNT-only queries cannot apply
        FilterExpression server-side without scanning attributes, so we
        fetch the items via the same projection the cap path needs and
        count active ones in Python. The team-lead-index entries per
        owner are bounded (the cap itself is the bound), so this stays
        O(limit) RCU.
        """
        resp = self._table.query(
            IndexName="team-lead-index",
            KeyConditionExpression=Key("team_lead_user_id").eq(owner_user_id),
            FilterExpression=Attr("status").ne("archived"),
            ProjectionExpression="tenant_id, #s",
            ExpressionAttributeNames={"#s": "status"},
        )
        return int(resp.get("Count", 0))

    def list_all(self, *, cursor: Optional[dict[str, Any]] = None, limit: int = 100) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """Fetch all tenants via Scan (admin only; limit<=100 enforced by the caller)."""
        kwargs: dict[str, Any] = {"Limit": min(limit, 100)}
        if cursor:
            kwargs["ExclusiveStartKey"] = cursor
        resp = self._table.scan(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    # ----- write -----
    def create(
        self,
        *,
        name: str,
        team_lead_user_id: str,
        default_credit: Optional[int] = None,
        created_by: str,
        tenant_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create a new tenant. Raises ConditionalCheckFailed if the tenant_id already exists."""
        # Check the team lead cap (admin-owned tenants are exempt).
        if team_lead_user_id != ADMIN_OWNED:
            existing = self.count_by_owner(team_lead_user_id)
            if existing >= self.TEAM_LEAD_TENANT_LIMIT:
                raise TenantLimitExceededError(
                    f"Team lead {team_lead_user_id} already owns {existing} tenants "
                    f"(limit={self.TEAM_LEAD_TENANT_LIMIT})"
                )

        tid = tenant_id or f"tenant-{uuid4()}"
        now = _now_iso()
        item: dict[str, Any] = {
            "tenant_id": tid,
            "name": name,
            "team_lead_user_id": team_lead_user_id,
            "default_credit": Decimal(default_credit or _default_credit_fallback()),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(tenant_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Tenant already exists: tenant_id={tid}")
            raise
        return item

    def update(
        self,
        *,
        tenant_id: str,
        name: Optional[str] = None,
        default_credit: Optional[int] = None,
        bound_mode: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update name / default_credit / bound_mode only. team_lead_user_id is
        updated via set_owner. `bound_mode` (see VALID_BOUND_MODES) is
        validated here — an admin typo must 400 at the write, not silently sit
        on the row unread until `resolve_bound_mode`'s "anything unrecognised
        falls back to strict" swallows it forever."""
        updates: list[str] = []
        values: dict[str, Any] = {":now": _now_iso(), ":active": "active"}
        expr_names: dict[str, str] = {"#s": "status"}
        if name is not None:
            updates.append("#n = :n")
            expr_names["#n"] = "name"
            values[":n"] = name
        if default_credit is not None:
            updates.append("default_credit = :dc")
            values[":dc"] = Decimal(default_credit)
        if bound_mode is not None:
            if bound_mode not in VALID_BOUND_MODES:
                raise ValueError(
                    f"bound_mode must be one of {sorted(VALID_BOUND_MODES)}, got {bound_mode!r}"
                )
            updates.append("bound_mode = :bm")
            values[":bm"] = bound_mode
        if not updates:
            existing = self.get(tenant_id)
            if not existing:
                raise TenantNotFoundError(tenant_id)
            return existing
        updates.append("updated_at = :now")

        try:
            resp = self._table.update_item(
                Key={"tenant_id": tenant_id},
                UpdateExpression="SET " + ", ".join(updates),
                ExpressionAttributeValues=values,
                ExpressionAttributeNames=expr_names,
                ConditionExpression="attribute_exists(tenant_id) AND #s = :active",
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TenantNotFoundError(tenant_id)
            raise
        return resp.get("Attributes", {})

    def set_owner(self, *, tenant_id: str, new_owner_user_id: str) -> dict[str, Any]:
        """Reassign a tenant orphaned by Cognito user deletion/recreation (v2.1 C-C)."""
        try:
            resp = self._table.update_item(
                Key={"tenant_id": tenant_id},
                UpdateExpression="SET team_lead_user_id = :o, updated_at = :now",
                ExpressionAttributeValues={
                    ":o": new_owner_user_id,
                    ":now": _now_iso(),
                },
                ConditionExpression="attribute_exists(tenant_id)",
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise TenantNotFoundError(tenant_id)
            raise
        return resp.get("Attributes", {})

    def archive(self, tenant_id: str) -> None:
        """Archive a tenant (status=archived) and all its UserTenants rows.

        P2-2 regression: archiving a Tenant used to leave UserTenants
        rows with ``status=active``, which meant ``reserve()`` and
        ``refund()`` against the archived tenant would still succeed and
        rack up Bedrock usage on a tenant that was "deleted". The
        user-facing `/v1/messages` call would happily drain the old
        budget until an admin noticed.

        Archival is now a two-phase operation:
          1. Scan the user_tenants table for rows targeting this tenant
             and flip each active one to status=archived.
          2. Flip the tenants row itself to status=archived.

        We intentionally do steps 1 → 2 (and not the other way around)
        so that if the scan fails we leave the tenant in a re-runnable
        state. The reverse order would leave the tenant dead but its
        members writable.
        """
        from boto3.dynamodb.conditions import Attr

        from .client import user_tenants_table_name, get_dynamodb_resource

        ut_table = get_dynamodb_resource().Table(user_tenants_table_name())
        now = _now_iso()

        # Scan is acceptable here — archival is a rare, admin-initiated
        # operation. For a high-tenant-count deployment this can be
        # upgraded to a GSI query later.
        last_evaluated: Optional[dict[str, Any]] = None
        while True:
            scan_kwargs: dict[str, Any] = {
                "FilterExpression": Attr("tenant_id").eq(tenant_id)
                & (Attr("status").eq("active") | Attr("status").not_exists()),
                "ProjectionExpression": "user_id, tenant_id",
            }
            if last_evaluated:
                scan_kwargs["ExclusiveStartKey"] = last_evaluated
            resp = ut_table.scan(**scan_kwargs)
            for row in resp.get("Items", []):
                ut_table.update_item(
                    Key={
                        "user_id": row["user_id"],
                        "tenant_id": row["tenant_id"],
                    },
                    UpdateExpression="SET #s = :archived, updated_at = :now",
                    ConditionExpression=(
                        "attribute_not_exists(#s) OR #s = :active"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={
                        ":archived": "archived",
                        ":active": "active",
                        ":now": now,
                    },
                )
            last_evaluated = resp.get("LastEvaluatedKey")
            if not last_evaluated:
                break

        # Finally flip the tenant itself.
        self._table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET #s = :archived, updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":archived": "archived",
                ":now": now,
            },
        )

    # ------------------------------------------------------------------
    # Seed (idempotent)
    # ------------------------------------------------------------------
    def seed_default(
        self,
        *,
        tenant_id: str = "default-org",
        name: str = "Default Organization",
        default_credit: Optional[int] = None,
        created_by: str = "system-seed",
    ) -> dict[str, Any]:
        """Idempotently put the default tenant for OSS zero-touch startup.

        Idempotency: ConditionExpression='attribute_not_exists(tenant_id)' ensures
        no write occurs if the record already exists, and it is never touched.

        Returns: {"tenant_id": str, "created": bool, "item": dict}
          - created=True: newly created by this call
          - created=False: already existed (no-op)
        """
        # If a record exists (even archived), return it without touching it.
        existing = self.get_including_archived(tenant_id)
        if existing:
            return {"tenant_id": tenant_id, "created": False, "item": existing}

        now = _now_iso()
        item: dict[str, Any] = {
            "tenant_id": tenant_id,
            "name": name,
            # At the time of first seed, no users (including admins) exist yet,
            # so we use the ADMIN_OWNED sentinel (exempt from the cap, treated as admin-owned).
            "team_lead_user_id": ADMIN_OWNED,
            "default_credit": Decimal(default_credit or _default_credit_fallback()),
            "status": "active",
            "created_at": now,
            "updated_at": now,
            "created_by": created_by,
        }
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(tenant_id)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # Race condition: another process seeded first — no-op.
                existing = self.get_including_archived(tenant_id) or item
                return {"tenant_id": tenant_id, "created": False, "item": existing}
            raise
        return {"tenant_id": tenant_id, "created": True, "item": item}

    # ------------------------------------------------------------------
    # PR 3: the per-user dollar default and its seal (P3.2/P3.3)
    # ------------------------------------------------------------------
    def set_user_dollar_default(
        self, *, tenant_id: str, effective_period: str, amount_microusd: int
    ) -> dict[str, Any]:
        """Set the per-user money default that takes effect FROM
        `effective_period` onward, pruning both maps in the same write
        (Amendment 3).

        The cheap check -- refusing an `effective_period` at or before this
        process's own `current_period()` -- exists only to fail fast on the
        common mistake (backdating a default). It is NOT the guarantee: a
        clock-skewed caller could still name a period that has *since* been
        sealed by the time this write lands, and that is exactly what the
        row-side condition below catches. **The guarantee is the condition,
        not the check** (I3): `attribute_not_exists(sealed_user_dollar_base.#p)`
        is what actually stops a setter from changing a sealed period, the
        check above only stops an obviously-wrong call one round trip earlier.

        Retries on a lost race exactly like every other CAS writer in the
        tenant pool's own repository module: a `ConditionalCheckFailedException`
        here means either the period was sealed between our read and this
        write, or the map attribute's very existence changed under us (see
        the vivify branch below) -- either way a fresh read tells the two
        apart, so we re-read and retry rather than assume which one happened.
        """
        effective_period = str(effective_period)
        amount = int(amount_microusd)
        if effective_period <= current_period():
            raise ValueError(
                f"effective_period {effective_period!r} must be strictly after "
                f"the current period ({current_period()!r}); a default cannot "
                f"be backdated onto a period that may already be sealed. (This "
                f"is the cheap check, not the guarantee -- see this method's "
                f"own docstring.)"
            )
        for _attempt in range(_SEAL_MAX_RETRIES):
            item = self.get(tenant_id, consistent_read=True)
            if item is None:
                raise TenantNotFoundError(tenant_id)
            sealed = _sealed_map(item)
            if effective_period in sealed:
                raise SealedPeriodError(
                    f"{effective_period!r} is already sealed for {tenant_id!r} "
                    f"at {sealed[effective_period]} micro-USD; a sealed period's "
                    f"base cannot change (G6)."
                )
            defaults = _defaults_map(item)
            to_prune = _defaults_periods_to_prune(
                defaults.keys(), current_period=current_period())
            names: dict[str, str] = {"#p": effective_period}
            values: dict[str, Any] = {":now": _now_iso()}
            removes: list[str] = []
            for i, p in enumerate(to_prune):
                alias = f"#dp{i}"
                names[alias] = p
                removes.append(f"user_dollar_defaults.{alias}")
            set_clauses = ["updated_at = :now"]
            cond = ["attribute_not_exists(sealed_user_dollar_base.#p)"]
            if USER_DOLLAR_DEFAULTS_ATTR in item:
                # The common case: the map already exists, so a nested-path SET
                # is legal DynamoDB (a SET on `mapAttr.key` when `mapAttr` itself
                # is absent raises ValidationException -- DynamoDB does not
                # vivify an intermediate map, only a top-level attribute via ADD).
                values[":amt"] = Decimal(amount)
                set_clauses.append("user_dollar_defaults.#p = :amt")
                set_clauses.append(
                    "user_dollar_defaults_version = "
                    "if_not_exists(user_dollar_defaults_version, :zero) + :one")
                values[":zero"] = Decimal(0)
                values[":one"] = Decimal(1)
                cond.append("attribute_exists(user_dollar_defaults)")
            else:
                # First-ever default for this tenant (or a legacy row from
                # before this field existed): vivify the whole map in one SET
                # rather than a nested path, and start the version at 1.
                set_clauses.append("user_dollar_defaults = :m")
                set_clauses.append("user_dollar_defaults_version = :one")
                values[":m"] = {effective_period: Decimal(amount)}
                values[":one"] = Decimal(1)
                cond.append("attribute_not_exists(user_dollar_defaults)")
            update_expr = "SET " + ", ".join(set_clauses)
            if removes:
                update_expr += " REMOVE " + ", ".join(removes)
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id},
                    UpdateExpression=update_expr,
                    ConditionExpression=" AND ".join(cond),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") \
                        != "ConditionalCheckFailedException":
                    raise
                continue  # sealed under us, or the map's existence flipped -> retry
            return self.get(tenant_id) or {}

        raise RuntimeError(
            f"set_user_dollar_default: lost the CAS {_SEAL_MAX_RETRIES}x for "
            f"{tenant_id}/{effective_period}; concurrent writers on one tenant row")

    def seal_user_dollar_base(
        self, tenant_id: str, period: str
    ) -> Optional[int]:
        """Fix the per-user money base for `period`, the first time any
        admission needs it, and return the value in force -- sealed by THIS
        call, sealed already by an earlier one, or `None` when the tenant has
        never configured a default (I3: absence means unconfigured, never an
        error -- raising here would wedge every admission for this tenant).

        **The version clause is not optional.** `attribute_not_exists(seal.#p)`
        alone only proves no seal exists; it says nothing about whether the
        history THIS call read is still the history in force. Without the
        `user_dollar_defaults_version = :v_read` clause, this interleaving
        loses a write silently: an admission resolves 100 for P; the setter
        writes 80 for P and its `attribute_not_exists` condition succeeds,
        reporting success to the operator; this call then seals 100. Every
        later admission agrees with 100, and the operator was told 80 took
        effect. The version clause makes the seal a compare-and-set on what
        was actually read, so the setter's write (which also bumps the
        version) always wins that race if it lands first.
        """
        for _attempt in range(_SEAL_MAX_RETRIES):
            item = self.get(tenant_id, consistent_read=True)
            if item is None:
                return None
            sealed = _sealed_map(item)
            if period in sealed:
                return int(sealed[period])
            resolved = resolve_user_dollar_default(item, period)
            if resolved is None:
                # Unconfigured. Nothing to seal, nothing to admit against --
                # the caller (mvp.reserve_limits' configured_when) reads this
                # exact return to decide the wall contributes no item.
                return None
            version_read = int(item.get(USER_DOLLAR_DEFAULTS_VERSION_ATTR, 0) or 0)
            to_prune = _seals_periods_to_prune(
                sealed.keys(), current_period=current_period())
            names: dict[str, str] = {}
            values: dict[str, Any] = {
                ":v_read": Decimal(version_read),
                ":now": _now_iso(),
            }
            removes: list[str] = []
            for i, p in enumerate(to_prune):
                alias = f"#sp{i}"
                names[alias] = p
                removes.append(f"sealed_user_dollar_base.{alias}")
            set_clauses = ["updated_at = :now"]
            cond = ["user_dollar_defaults_version = :v_read"]
            if SEALED_USER_DOLLAR_BASE_ATTR in item:
                names["#p"] = period
                values[":v"] = Decimal(int(resolved))
                set_clauses.append("sealed_user_dollar_base.#p = :v")
                cond.append("attribute_not_exists(sealed_user_dollar_base.#p)")
            else:
                set_clauses.append("sealed_user_dollar_base = :m")
                values[":m"] = {period: Decimal(int(resolved))}
                cond.append("attribute_not_exists(sealed_user_dollar_base)")
            update_expr = "SET " + ", ".join(set_clauses)
            if removes:
                update_expr += " REMOVE " + ", ".join(removes)
            try:
                self._table.update_item(
                    Key={"tenant_id": tenant_id},
                    UpdateExpression=update_expr,
                    ConditionExpression=" AND ".join(cond),
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues=values,
                )
                return resolved
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") \
                        != "ConditionalCheckFailedException":
                    raise
                continue  # someone else sealed it, or the version moved -> re-read

        raise RuntimeError(
            f"seal_user_dollar_base: lost the seal CAS {_SEAL_MAX_RETRIES}x for "
            f"{tenant_id}/{period}; sustained concurrent writers on one tenant row")


# ---------------------------------------------------------------------------
# PR 3: pure functions over a tenant row (P3.2/P3.3), and the retry bound the
# two CAS methods above share. Kept pure and free of any DynamoDB call so a
# caller holding a row it already read (the admission path's OWN snapshot,
# I5) can resolve/decide without a second read -- see
# `mvp.routing.user_dollar_quota` and `mvp.reserve_limits`'s `configured_when`.
# ---------------------------------------------------------------------------
USER_DOLLAR_DEFAULTS_ATTR = "user_dollar_defaults"
SEALED_USER_DOLLAR_BASE_ATTR = "sealed_user_dollar_base"
USER_DOLLAR_DEFAULTS_VERSION_ATTR = "user_dollar_defaults_version"

# How many periods a sealed base is kept before Amendment 3 prunes it, and the
# floor below which a stale default entry (superseded by a later effective
# period, and not the latest-effective one) is pruned too. A CONSTANT here
# rather than per-tenant configuration: the row's worst-case size is derived
# from it (`worst_case_tenant_row_extra_bytes` below), and a tenant-configurable
# horizon would make that bound a variable nothing could size against.
_SEAL_RETENTION_PERIODS = 12

_SEAL_MAX_RETRIES = 8


class SealedPeriodError(ValueError):
    """A setter tried to change a period `seal_user_dollar_base` already fixed.

    G6: a sealed period's base cannot change. Raised by the cheap check's
    row-side twin (the `attribute_not_exists(sealed_user_dollar_base.#p)`
    condition) so a caller gets a typed reason rather than a bare CAS-retries-
    exhausted `RuntimeError` -- this one is not a race, it is a refusal."""


def _sealed_map(item: dict[str, Any]) -> dict[str, int]:
    raw = (item or {}).get(SEALED_USER_DOLLAR_BASE_ATTR) or {}
    return {str(k): int(v) for k, v in raw.items()}


def _defaults_map(item: dict[str, Any]) -> dict[str, int]:
    raw = (item or {}).get(USER_DOLLAR_DEFAULTS_ATTR) or {}
    return {str(k): int(v) for k, v in raw.items()}


def resolve_user_dollar_default(item: Optional[dict[str, Any]], period: str) -> Optional[int]:
    """The `user_dollar_defaults` entry with the greatest key `<= period`, or
    `None` when the tenant has no entry effective by `period` (I3: absence
    means unconfigured, not an error -- the caller must not raise on this)."""
    defaults = _defaults_map(item or {})
    candidates = [p for p in defaults if p <= period]
    if not candidates:
        return None
    return defaults[max(candidates)]


def sealed_user_dollar_base(item: Optional[dict[str, Any]], period: str) -> Optional[int]:
    """The value ALREADY sealed for `period` on this row, or `None` if this
    exact period has not been sealed (whether or not a default exists for
    it -- sealing is a distinct fact from configuration)."""
    sealed = _sealed_map(item or {})
    return sealed.get(period)


def _period_n_before(period: str, n: int) -> str:
    """`period` minus `n` calendar months ("2026-07", 12) -> "2025-07"."""
    year, month = (int(x) for x in period.split("-"))
    total = year * 12 + (month - 1) - int(n)
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _seals_periods_to_prune(sealed_periods, *, current_period: str) -> list[str]:
    """Every sealed period older than the retention horizon, from `current_period`.

    Conservative by construction: computed from a snapshot the caller just
    read, and only ever names periods THAT snapshot showed -- a period sealed
    by someone else after our read simply is not in this list, so this can
    under-prune (leaving one more entry for the next writer to catch) but can
    never remove a period a concurrent seal just created.
    """
    floor = _period_n_before(current_period, _SEAL_RETENTION_PERIODS)
    return [p for p in sealed_periods if p < floor]


def _defaults_periods_to_prune(default_periods, *, current_period: str) -> list[str]:
    """Every default entry that is neither the latest ALREADY-EFFECTIVE one
    (greatest key `<= current_period`) nor a FUTURE one (`> current_period`).

    The latest-effective entry must survive pruning or a later period would
    resolve to nothing (Amendment 3's own requirement) -- so it is computed
    and excluded explicitly rather than assumed to be the max of the pruned
    set. Same conservative-snapshot property as `_seals_periods_to_prune`.
    """
    keys = list(default_periods)
    effective = [p for p in keys if p <= current_period]
    latest_effective = max(effective) if effective else None
    return [p for p in keys if p <= current_period and p != latest_effective]


def worst_case_tenant_row_extra_bytes(*, max_microusd_digits: int = 15) -> int:
    """The widest `user_dollar_defaults` + `sealed_user_dollar_base` can make
    this row, derived from `_SEAL_RETENTION_PERIODS` rather than hardcoded
    (Amendment 3) -- the same discipline the tenant pool's own
    `worst_case_pool_item_bytes` uses for the pool row, so a change to the
    retention horizon moves this bound with it instead of leaving a stale
    number beside a different one.

    Bounds BOTH maps at `_SEAL_RETENTION_PERIODS` entries: seals are pruned to
    that many periods outright, and defaults keep at most one already-
    effective entry plus (at most, in practice) a small number of future ones
    -- bounding defaults at the same horizon is deliberately generous rather
    than exact, since nothing stops an operator scheduling many future
    defaults before any of them seals. `max_microusd_digits=15` covers
    999,999,999.999999 USD in micro-USD, wider than any figure this deployment
    validates elsewhere (see `limits.MAX_POOL_BUDGET_USD_CENTS`) -- a caller
    with a tighter known bound may pass a smaller value.
    """
    period_key_bytes = len("YYYY-MM")
    per_entry = period_key_bytes + max_microusd_digits
    two_maps = 2 * _SEAL_RETENTION_PERIODS * per_entry
    return two_maps + len(USER_DOLLAR_DEFAULTS_ATTR) + len(SEALED_USER_DOLLAR_BASE_ATTR)


def resolve_bound_mode(tenant_id: Optional[str], *, repo: Optional["TenantsRepository"] = None) -> str:
    """The reservation-bound mode in force for `tenant_id`: `"strict"` or
    `"calibrated"` (see VALID_BOUND_MODES). The single decision point the
    reserve chokepoint (`mvp/_pipeline.py`) and the read-only admin view both
    consult, so a tenant's mode is one fact, not two independently-drifting
    reads.

    Fails closed to `"strict"` on every "we don't actually know" path: no
    `tenant_id`, no Tenants row, a row with no `bound_mode` attribute (an
    existing tenant from before this change), or an unrecognised value on the
    row (a hand-edited/corrupt attribute). None of those are "the operator
    asked for calibrated" — calibrated is an opt-in the contract requires be
    backed by a real measurement, so anything short of an explicit, valid
    value on the row must resolve to the bound that needs no measurement to
    be correct.
    """
    if not tenant_id:
        return BOUND_MODE_STRICT
    try:
        item = (repo or TenantsRepository()).get(tenant_id)
    except Exception:  # noqa: BLE001 — a lookup failure must never crash reserve;
        # fail to the safe mode instead of raising into the money path.
        return BOUND_MODE_STRICT
    if not item:
        return BOUND_MODE_STRICT
    mode = item.get("bound_mode")
    return mode if mode in VALID_BOUND_MODES else BOUND_MODE_STRICT
