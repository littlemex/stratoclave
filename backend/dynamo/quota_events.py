"""QuotaEvents: the raise request, the approval's grant, and the daily slot.

ONE table holds three kinds of row, because all three are read together on the
paths that matter and none of them is on the money hot path:

    Daily slot   PK `USER#<user_id>`      SK `SLOT#<tenant_id>#<yyyy-mm-dd>`
    Request      PK `REQUEST#<id>`        SK `REQUEST`
    Grant        PK `TENANT#<tenant_id>`  SK `GRANT#<grant_id>`

THIS MODULE IS STORAGE AND NOTHING ELSE. It builds keys, single writes and
transaction fragments; it decides no lifecycle question. In particular the
question "does this grant currently contribute to `pool_granted_microusd`" is a
lifecycle rule and lives in `mvp.grants.is_capacity_bearing`, its only
definition -- not here, and not re-exported from here. The predicate this module
DOES own is a different one: `grant_status = ACTIVE` is an INDEX membership
condition, which is how the sweeper finds work, and that belongs to storage.

WHY THE GRANT ROW CARRIES A BARE `tenant_id`. `tenant-status-index` partitions
on it, and a caller holding a tenant id would be unable to query an index
partitioned on `TENANT#<id>` without knowing to prepend the prefix -- so the
index attribute is the RAW id and the partition key keeps the prefix. The two are
deliberately different attributes rather than one doing both jobs.

WHY THE EXPIRY INDEX IS SPARSE, AND WHY THAT IS THE MECHANISM RATHER THAN A
FILTER. `grant-expiry-index` partitions on `grant_status`, an attribute written
ONLY while the grant is ACTIVE and REMOVED in the same transaction that makes the
grant terminal. A revoked grant therefore leaves the index by construction: the
sweeper's next Query cannot see it, so exactly-once revocation does not depend on
a filter the sweeper might get wrong, and the index holds only the rows the
sweeper has work for rather than every grant that ever existed. Slot rows never
write `tenant_id` either, which keeps `tenant-status-index` sparse to requests
and grants for the same reason.

WHY THE POOL-SIDE WRITES ARE NOT HERE. A grant moves three attributes on the
`TenantBudgets` row, and those fragments live on `TenantBudgetsRepository`
alongside every other writer of that row -- the module whose declaration says who
writes the ceiling. A repository for one table building conditional writes against
another is how a second authority over a row's shape appears, which is the exact
failure `dynamo/pool_row_schema.py` exists to prevent.

Amounts are integer micro-USD; this module never introduces a float.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .client import get_dynamodb_resource, quota_events_table_name

# --- request lifecycle ----------------------------------------------------
STATUS_PENDING = "PENDING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_WITHDRAWN = "WITHDRAWN"

# --- grant lifecycle ------------------------------------------------------
GRANT_ACTIVE = "ACTIVE"
GRANT_EXPIRED = "EXPIRED"
GRANT_REVOKED = "REVOKED"
#: A grant whose subtraction could not be made to commit. It keeps its share of
#: `pool_granted_microusd` -- honestly, because the capacity was never actually
#: given back -- and it leaves the expiry index so it is not retried forever.
GRANT_REVOKE_BLOCKED = "REVOKE_BLOCKED"

#: How many times a sweep may fail to revoke one grant for a reason OTHER than
#: "somebody else already did" before the grant is marked blocked and alarmed. A
#: bound rather than a retry-forever, because one poison grant must not consume
#: every run for the rest of the deployment's life.
MAX_REVOKE_ATTEMPTS = 5

#: The name of the sparse index attribute. Written only while ACTIVE; `REMOVE`d
#: in the same transaction as every terminal transition.
GRANT_STATUS_ATTR = "grant_status"

TENANT_STATUS_INDEX = "tenant-status-index"
GRANT_EXPIRY_INDEX = "grant-expiry-index"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slot_date_str(now_epoch: int) -> str:
    """The UTC calendar day a submission at `now_epoch` falls in."""
    return datetime.fromtimestamp(int(now_epoch), tz=timezone.utc).strftime("%Y-%m-%d")


def slot_reset_at(date_str: str) -> str:
    """When the day named by `date_str` releases its slot, WITH ITS ZONE.

    The zone is explicit and not implied. A refusal that says "resets at
    00:00:00" is read in the reader's own timezone and is then wrong for most of
    the world by up to a day, which for a once-a-day allowance is the whole
    allowance.
    """
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return (day + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_created_at(status: str, created_at: str) -> str:
    """`tenant-status-index`'s sort key: the status and the creation instant in
    one string, so an approver's list is "this tenant's PENDING requests, oldest
    first" as a key range rather than a filtered scan."""
    return f"{status}#{created_at}"


class QuotaEventsRepository:
    def __init__(self, table_name: Optional[str] = None) -> None:
        self._name = table_name or quota_events_table_name()
        self._table = get_dynamodb_resource().Table(self._name)

    @property
    def table_name(self) -> str:
        return self._name

    # ----- keys -----
    @staticmethod
    def slot_key(user_id: str, tenant_id: str, date_str: str) -> dict[str, Any]:
        return {"pk": f"USER#{user_id}", "sk": f"SLOT#{tenant_id}#{date_str}"}

    @staticmethod
    def request_key(request_id: str) -> dict[str, Any]:
        return {"pk": f"REQUEST#{request_id}", "sk": "REQUEST"}

    @staticmethod
    def grant_key(tenant_id: str, grant_id: str) -> dict[str, Any]:
        return {"pk": f"TENANT#{tenant_id}", "sk": f"GRANT#{grant_id}"}

    # ------------------------------------------------------------------
    # The daily slot, which is also the client token's anchor
    # ------------------------------------------------------------------
    # ONE row does both jobs, and that is the design rather than a saving. A
    # separate idempotency record and a separate daily counter would be two rows
    # that can disagree: a retry that created a second request while the counter
    # said one, or a counter at one with no request to point at. Because the slot
    # IS the record of what was admitted today, "the same token twice" and "a
    # second request today" are answered by the same conditional write.

    def put_slot_if_absent(
        self, *, user_id: str, tenant_id: str, date_str: str,
        client_token: str, request_id: str,
    ) -> bool:
        """Claim today's slot for `(user_id, tenant_id)`. True iff this call
        claimed it; False means somebody (possibly this same caller retrying)
        got there first and the stored slot is the authority.

        The token is stored as given. It is a caller-supplied idempotency key and
        R13 keeps it out of every log, metric, key and error body -- but the
        comparison the slot exists to make is equality against what was sent, so
        the stored value is the value.
        """
        item = {
            **self.slot_key(user_id, tenant_id, date_str),
            "client_token": str(client_token),
            "request_id": str(request_id),
            "user_id": str(user_id),
            # Deliberately NOT the bare `tenant_id` attribute: that one is
            # `tenant-status-index`'s partition key, and writing it here would
            # put every slot row into an index that exists for requests and
            # grants. The tenant is already in the sort key.
            "slot_tenant_id": str(tenant_id),
            "created_at": _now_iso(),
        }
        try:
            self._table.put_item(
                Item=item, ConditionExpression="attribute_not_exists(pk)")
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def get_slot(
        self, *, user_id: str, tenant_id: str, date_str: str,
        consistent_read: bool = True,
    ) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(
            Key=self.slot_key(user_id, tenant_id, date_str),
            ConsistentRead=consistent_read)
        return resp.get("Item")

    def delete_slot(self, *, user_id: str, tenant_id: str, date_str: str) -> None:
        """Release today's slot. Unconditional, and safe only because the
        service layer has already established that the request the slot names is
        DECIDED and its grant is no longer bearing capacity -- see R22. A
        condition here would be guarding the wrong fact: the slot's own contents
        cannot say whether the request it points at has been decided."""
        self._table.delete_item(
            Key=self.slot_key(user_id, tenant_id, date_str))

    # ------------------------------------------------------------------
    # The request
    # ------------------------------------------------------------------
    def put_request(
        self, *, request_id: str, tenant_id: str, user_id: str,
        asked_amount_microusd: int, reason_code: str, comment: Optional[str],
        limit_kind: str, created_at: Optional[str] = None,
        observed_limit_microusd: Optional[int] = None,
        observed_remaining_microusd: Optional[int] = None,
        observed_at: Optional[str] = None,
    ) -> dict[str, Any]:
        """Write a PENDING request and return it.

        The client token is NOT copied here. It lives on the slot row, which is
        the one row whose job is to answer "was this token already admitted"; a
        second copy would be a second place a caller-supplied secret is stored
        and a second thing to keep out of every sink R13 names.

        `observed_limit_microusd`/`observed_remaining_microusd`/`observed_at`
        (R30) are the tenant's pool position AS READ AT FILING TIME -- the
        one fact only the requester's own submission can capture, because an
        approver reading the request hours later sees the tenant's position
        NOW, not what she saw when she asked. `observed_remaining_microusd`
        is the SIGNED, non-clamped figure
        (`dynamo.tenant_budgets.pool_summary`'s own `remaining_microusd`):
        a deficit at filing time is exactly the fact a raise is filed to
        fix, and clamping it to zero here would be the same defect this
        whole change treats as a bug everywhere else it appears. All three
        are optional and omitted together (never partially) when the
        submission has no pool row to observe -- `comment`'s own pattern,
        extended to a triple.
        """
        now = created_at or _now_iso()
        item: dict[str, Any] = {
            **self.request_key(request_id),
            "request_id": str(request_id),
            # The BARE id: `tenant-status-index` partitions on it, and a caller
            # holding a tenant id must be able to query that index without
            # knowing about the `TENANT#` prefix.
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "status": STATUS_PENDING,
            "status_created_at": status_created_at(STATUS_PENDING, now),
            "asked_amount_microusd": Decimal(int(asked_amount_microusd)),
            "reason_code": str(reason_code),
            "limit_kind": str(limit_kind),
            "created_at": now,
            "revision": Decimal(1),
        }
        if comment:
            item["comment"] = str(comment)
        if observed_limit_microusd is not None and observed_remaining_microusd is not None:
            item["observed_limit_microusd"] = Decimal(int(observed_limit_microusd))
            # SIGNED: a negative deficit is a real, intended value, never
            # coerced towards zero here or anywhere it is later read.
            item["observed_remaining_microusd"] = Decimal(int(observed_remaining_microusd))
            item["observed_at"] = str(observed_at or now)
        self._table.put_item(
            Item=item, ConditionExpression="attribute_not_exists(pk)")
        return item

    def get_request(
        self, request_id: str, *, consistent_read: bool = True
    ) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(
            Key=self.request_key(request_id), ConsistentRead=consistent_read)
        return resp.get("Item")

    def list_requests_for_tenant(
        self, *, tenant_id: str, status: Optional[str] = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """An approver's queue, through `tenant-status-index`.

        `status` narrows by key range rather than by filter, which is what makes
        "the PENDING ones" cheap on a tenant with a long decided history.
        """
        cond = Key("tenant_id").eq(tenant_id)
        if status:
            cond = cond & Key("status_created_at").begins_with(f"{status}#")
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "IndexName": TENANT_STATUS_INDEX,
            "KeyConditionExpression": cond,
        }
        while len(out) < limit:
            resp = self._table.query(**kwargs)
            out.extend(r for r in resp.get("Items", []) if "request_id" in r)
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out[:limit]

    def decide_request_txn_item(
        self, *, request_id: str, to_status: str, decided_by: str,
        decided_at: str, read_revision: int,
        decision_comment: Optional[str] = None,
        approved_amount_microusd: Optional[int] = None,
        grant_id: Optional[str] = None,
        expires_at_epoch: Optional[int] = None,
    ) -> dict[str, Any]:
        """Transaction fragment moving a request out of PENDING, exactly once.

        The CAS is on `status` AND `revision`: status alone would let two
        decisions of the same request both believe they were the first if one of
        them retried after a partial view, and the revision is what makes the
        write ordered rather than merely conditional.
        """
        sets = [
            "#st = :to", "status_created_at = :sca", "decided_by = :by",
            "decided_at = :at", "revision = :next",
        ]
        values: dict[str, Any] = {
            ":to": {"S": to_status},
            ":sca": {"S": status_created_at(to_status, decided_at)},
            ":by": {"S": str(decided_by)},
            ":at": {"S": decided_at},
            ":next": {"N": str(int(read_revision) + 1)},
            ":pending": {"S": STATUS_PENDING},
            ":rev": {"N": str(int(read_revision))},
        }
        if decision_comment:
            sets.append("decision_comment = :dc")
            values[":dc"] = {"S": str(decision_comment)}
        if approved_amount_microusd is not None:
            sets.append("approved_amount_microusd = :amt")
            values[":amt"] = {"N": str(int(approved_amount_microusd))}
        if grant_id:
            sets.append("grant_id = :gid")
            values[":gid"] = {"S": str(grant_id)}
        if expires_at_epoch is not None:
            sets.append("expires_at = :exp")
            values[":exp"] = {"N": str(int(expires_at_epoch))}
        key = self.request_key(request_id)
        return {
            "Update": {
                "TableName": self._name,
                "Key": {"pk": {"S": key["pk"]}, "sk": {"S": key["sk"]}},
                "UpdateExpression": "SET " + ", ".join(sets),
                "ConditionExpression": "#st = :pending AND revision = :rev",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": values,
            }
        }

    # ------------------------------------------------------------------
    # The grant
    # ------------------------------------------------------------------
    def grant_put_txn_item(
        self, *, tenant_id: str, grant_id: str, request_id: str,
        approver_user_id: str, approved_amount_microusd: int,
        expires_at_epoch: int, target_pk: str, target_sk: str, period: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Transaction fragment creating an ACTIVE grant.

        `target_pk`, `target_sk` and `period` are PINNED here, copied from the
        pool row the approval actually read, and a revoke reads them back rather
        than recomputing the current period. A grant approved in July and revoked
        in August must reverse July's row; a revoke that asked
        `current_period()` would move a row the grant never raised, leaving
        July's ceiling permanently inflated and August's silently short.
        """
        key = self.grant_key(tenant_id, grant_id)
        return {
            "Put": {
                "TableName": self._name,
                "Item": {
                    "pk": {"S": key["pk"]},
                    "sk": {"S": key["sk"]},
                    "grant_id": {"S": str(grant_id)},
                    # Bare, for `tenant-status-index`.
                    "tenant_id": {"S": str(tenant_id)},
                    "request_id": {"S": str(request_id)},
                    "approver_user_id": {"S": str(approver_user_id)},
                    "status": {"S": GRANT_ACTIVE},
                    "status_created_at": {
                        "S": status_created_at(GRANT_ACTIVE, created_at)},
                    # The sparse expiry index's partition key. Present only
                    # while ACTIVE, so leaving the index is a REMOVE in the same
                    # transaction as the terminal transition rather than a
                    # follow-up write that can be lost.
                    GRANT_STATUS_ATTR: {"S": GRANT_ACTIVE},
                    "approved_amount_microusd": {
                        "N": str(int(approved_amount_microusd))},
                    "expires_at": {"N": str(int(expires_at_epoch))},
                    "target_pk": {"S": str(target_pk)},
                    "target_sk": {"S": str(target_sk)},
                    "period": {"S": str(period)},
                    "created_at": {"S": created_at},
                    "revision": {"N": "1"},
                    "revoke_attempts": {"N": "0"},
                },
                "ConditionExpression": "attribute_not_exists(pk)",
            }
        }

    def grant_terminal_txn_item(
        self, *, tenant_id: str, grant_id: str, to_status: str,
        approved_amount_read: int, revoked_by: str, revoked_at: str,
        revoke_reason: Optional[str] = None,
    ) -> dict[str, Any]:
        """Transaction fragment taking a grant to a terminal status.

        THE SOLE ARBITER OF EXACTLY-ONCE REVOCATION, and it is one condition
        doing two jobs. `status = ACTIVE` means only one of two racing sweeps can
        commit; `approved_amount_microusd = :read` means the amount subtracted
        from the pool in the paired fragment is the amount this row still holds,
        so a grant mutated between the read and the write cannot have a stale
        figure subtracted for it.

        `REMOVE grant_status` in the same fragment is what takes the grant out of
        the expiry index. Doing it here rather than afterwards is the difference
        between "revoked and invisible" and "revoked, and visible until a second
        write lands".
        """
        key = self.grant_key(tenant_id, grant_id)
        sets = ["#st = :to", "status_created_at = :sca", "revoked_by = :by",
                "revoked_at = :at", "revision = revision + :one"]
        values: dict[str, Any] = {
            ":to": {"S": to_status},
            ":sca": {"S": status_created_at(to_status, revoked_at)},
            ":by": {"S": str(revoked_by)},
            ":at": {"S": revoked_at},
            ":one": {"N": "1"},
            ":active": {"S": GRANT_ACTIVE},
            ":read": {"N": str(int(approved_amount_read))},
        }
        if revoke_reason:
            sets.append("revoke_reason = :rr")
            values[":rr"] = {"S": str(revoke_reason)}
        return {
            "Update": {
                "TableName": self._name,
                "Key": {"pk": {"S": key["pk"]}, "sk": {"S": key["sk"]}},
                "UpdateExpression": (
                    f"SET {', '.join(sets)} REMOVE {GRANT_STATUS_ATTR}"),
                "ConditionExpression": (
                    "#st = :active AND approved_amount_microusd = :read"),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": values,
            }
        }

    def get_grant(
        self, *, tenant_id: str, grant_id: str, consistent_read: bool = True
    ) -> Optional[dict[str, Any]]:
        resp = self._table.get_item(
            Key=self.grant_key(tenant_id, grant_id), ConsistentRead=consistent_read)
        return resp.get("Item")

    def bump_revoke_attempts(self, *, tenant_id: str, grant_id: str) -> Optional[int]:
        """Record that a revoke attempt failed for a reason other than "someone
        else already did it". Returns the new count, or None when the grant is no
        longer ACTIVE (which means it was in fact revoked and there is nothing to
        count). Never raises for the row's state: a bookkeeping write must not be
        what fails a sweep."""
        try:
            resp = self._table.update_item(
                Key=self.grant_key(tenant_id, grant_id),
                UpdateExpression="ADD revoke_attempts :one SET updated_at = :now",
                ConditionExpression="#st = :active",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":one": Decimal(1), ":active": GRANT_ACTIVE,
                    ":now": _now_iso()},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return None
            raise
        v = (resp.get("Attributes") or {}).get("revoke_attempts")
        return None if v is None else int(v)

    def mark_revoke_blocked(
        self, *, tenant_id: str, grant_id: str, reason: str = "",
        max_attempts: int = MAX_REVOKE_ATTEMPTS,
    ) -> bool:
        """Take a grant that cannot be revoked out of the expiry index and mark
        it for a person.

        The pool is DELIBERATELY NOT TOUCHED. The capacity was never given back,
        so `pool_granted_microusd` still counting it is the honest state -- and
        `mvp.grants.is_capacity_bearing` says so, which is why the reconciler
        does not then report the row as drifting. What changes is only that the
        sweeper stops retrying it: `REMOVE grant_status` leaves the index, so one
        poison grant cannot consume every run from now on, and the alarm on
        `revoke_blocked_grants` is what brings a person to the runbook.

        A PER-GRANT FLAG AND A REASON ARE WRITTEN HERE, not only the metric, and
        the two are not substitutes. The metric is how an operator learns THAT
        something is stuck -- it is a count and cannot say more. `revoke_blocked`
        and `revoke_blocked_reason` on the row are how they learn WHICH grant and
        why, which is the only form of the answer that leads to a repair. The flag
        is written in the SAME update as the status it projects, so the two cannot
        disagree; it is redundant with `status` by construction and exists so an
        inventory can filter on it without encoding the lifecycle vocabulary.
        """
        try:
            self._table.update_item(
                Key=self.grant_key(tenant_id, grant_id),
                UpdateExpression=(
                    "SET #st = :blocked, status_created_at = :sca, "
                    "blocked_at = :now, revoke_blocked = :true, "
                    "revoke_blocked_reason = :reason, revision = revision + :one "
                    f"REMOVE {GRANT_STATUS_ATTR}"),
                ConditionExpression=(
                    "#st = :active AND revoke_attempts >= :max"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":blocked": GRANT_REVOKE_BLOCKED,
                    ":sca": status_created_at(GRANT_REVOKE_BLOCKED, _now_iso()),
                    ":now": _now_iso(),
                    ":true": True,
                    ":reason": str(reason or "unknown"),
                    ":one": Decimal(1),
                    ":active": GRANT_ACTIVE,
                    ":max": Decimal(int(max_attempts)),
                },
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def clear_revoke_block(self, *, tenant_id: str, grant_id: str) -> bool:
        """Put a blocked grant back in the sweeper's way, for a hand-repair.

        The runbook step: an operator who has fixed whatever refused the
        subtraction needs the grant to be revocable again, and the two things
        standing in the way are its status and its absence from the expiry index.
        This restores both and resets the attempt count, so the bound the block was
        reached through starts over rather than blocking again on the first try.

        The pool is untouched, as it was untouched on the way in. Nothing about the
        block moved money, so nothing about clearing it does either -- which is
        what makes this safe to run without knowing whether the original failure
        had partially applied.
        """
        try:
            self._table.update_item(
                Key=self.grant_key(tenant_id, grant_id),
                UpdateExpression=(
                    "SET #st = :active, status_created_at = :sca, "
                    f"{GRANT_STATUS_ATTR} = :active, revoke_attempts = :zero, "
                    "unblocked_at = :now, revision = revision + :one "
                    "REMOVE revoke_blocked, revoke_blocked_reason, blocked_at"),
                ConditionExpression="#st = :blocked",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":active": GRANT_ACTIVE,
                    ":sca": status_created_at(GRANT_ACTIVE, _now_iso()),
                    ":blocked": GRANT_REVOKE_BLOCKED,
                    ":zero": Decimal(0),
                    ":now": _now_iso(),
                    ":one": Decimal(1),
                },
            )
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise

    def list_active_grants_expiring(
        self, *, now_epoch: int, limit: int = 25,
        exclusive_start_key: Optional[dict[str, Any]] = None,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]]]:
        """One page of the sweeper's work: ACTIVE grants already past expiry.

        A pure key condition over the sparse index, with no FilterExpression.
        That matters for the same reason it matters on the hold reaper: DynamoDB
        applies `Limit` to items EVALUATED and a filter afterwards, so a filtered
        page can return nothing while work sits behind it. Here every item in the
        index is by construction an ACTIVE grant, and the sort key IS the expiry,
        so `Limit` counts only grants that are actually due, oldest first.

        Eventually consistent, because a GSI cannot be read consistently. That is
        a deliberate trade the sweeper can afford: a grant that has just become
        terminal is at worst visited once more, and the terminal transition's own
        condition refuses the second revoke.
        """
        kwargs: dict[str, Any] = {
            "IndexName": GRANT_EXPIRY_INDEX,
            "KeyConditionExpression": (
                Key(GRANT_STATUS_ATTR).eq(GRANT_ACTIVE)
                & Key("expires_at").lt(int(now_epoch))
            ),
            "Limit": int(limit),
        }
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        resp = self._table.query(**kwargs)
        return resp.get("Items", []), resp.get("LastEvaluatedKey")

    def list_grants_for_tenant(
        self, *, tenant_id: str,
        status: Optional[str] = None, period: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Every grant this tenant has ever held that matches the given
        filters, read to EXHAUSTION -- never truncated.

        The orphan hunt and the retirement drain both start HERE, from grants,
        rather than from pool rows. A sweep that starts from pool rows cannot see
        a grant whose target row is missing -- there is no row to start at -- so
        the one defect it most needs to find is the one it is structurally unable
        to.

        NO CAP. `revoke_all_active_grants` (R34's drain), `reconcile_tenant_grants`
        (the orphan hunt and the drift sum) and `_tenant_grants` (the reconciler's
        per-row check) all need EVERY grant a tenant has ever held: a grant this
        call dropped is a grant those three cannot revoke, reconcile or find --
        silently, because a paginated Query that stops early looks identical to a
        tenant that genuinely has fewer grants. The predecessor of this method
        capped itself at 500 for exactly this reason and broke exactly this
        guarantee once a tenant's lifetime grant count passed it: `sk` sorts by
        `grant_id`, not by time, so the grants left off the end were not
        reliably the oldest ones. A tenant with a long grant history costs
        proportionally more to read here, which is the honest price of the
        guarantee rather than a defect -- the alternative was a guarantee that
        silently stopped holding past 500 grants.

        `status`/`period`, when given, are a `FilterExpression` -- a narrowing of
        what is TRANSFERRED off this one partition, not of what is READ from it
        (DynamoDB applies a filter after paying for every item in the page). They
        exist for a caller that already knows it wants a small slice of a
        tenant's history: `_pipeline._expired_grant_reason_for_pool_wall` (a
        refusal-path lookup, cold but per-request) passes `status=GRANT_EXPIRED,
        period=<the refusing period>` so it is not carrying a tenant's entire
        grant history over the wire to answer a question about its most recent
        expiry. A tenant whose grants all fail the filter costs exactly what
        reading them costs either way.

        For a HUMAN reading a list on a screen, see `list_grants_for_tenant_page`
        below, which bounds itself and SAYS so rather than cutting silently.
        """
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("pk").eq(f"TENANT#{tenant_id}") & Key("sk").begins_with("GRANT#")
            ),
            "ConsistentRead": True,
        }
        filters = []
        if status is not None:
            filters.append(Attr("status").eq(status))
        if period is not None:
            filters.append(Attr("period").eq(period))
        if filters:
            expr = filters[0]
            for f in filters[1:]:
                expr = expr & f
            kwargs["FilterExpression"] = expr
        while True:
            resp = self._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return out

    def list_grants_for_tenant_page(
        self, *, tenant_id: str, limit: int = 500,
    ) -> tuple[list[dict[str, Any]], bool]:
        """A bounded PAGE of this tenant's grants, for a human reading a list on
        a screen -- never for a correctness path (see `list_grants_for_tenant`'s
        own docstring for why those must read to exhaustion instead).

        Returns `(items, truncated)`. `truncated` is True when this tenant holds
        more grants than `limit` names, so the caller's obligation is to SAY so
        (a `grants_truncated` field alongside the list) rather than let a reader
        believe the returned page is the tenant's whole history -- the silent cut
        `list_grants_for_tenant(limit=500)` used to make, with no signal anywhere
        in the response that one had happened.
        """
        out: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": (
                Key("pk").eq(f"TENANT#{tenant_id}") & Key("sk").begins_with("GRANT#")
            ),
            "ConsistentRead": True,
        }
        lek: Optional[dict[str, Any]] = None
        while True:
            resp = self._table.query(**kwargs)
            out.extend(resp.get("Items", []))
            lek = resp.get("LastEvaluatedKey")
            if len(out) >= limit or not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        truncated = len(out) > limit or bool(lek)
        return out[:limit], truncated

    def iter_all_grants(self, *, page_limit: int = 200):
        """Every grant row in the table, paginated.

        The reconciler's source for `pool_granted_microusd`: it needs the sum of
        capacity-bearing grants PER TARGET ROW across the whole fleet, gathered
        once per pass rather than once per row, which is what stops a reconciler
        from costing more the more carefully it looks.
        """
        kwargs: dict[str, Any] = {
            "FilterExpression": Attr("sk").begins_with("GRANT#"),
            "Limit": int(page_limit),
        }
        while True:
            resp = self._table.scan(**kwargs)
            for item in resp.get("Items", []):
                yield item
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                return
            kwargs["ExclusiveStartKey"] = lek

    # ------------------------------------------------------------------
    # The cross-table transaction this table anchors
    # ------------------------------------------------------------------
    def transact_write(self, items: list[dict[str, Any]]) -> None:
        """Commit a list of low-level transaction fragments.

        An approval spans three tables -- the authority check on `Tenants`, the
        request and grant rows here, and the pool row on `TenantBudgets` -- and
        it has to be one transaction or an approval could grant capacity a
        revoked approver was never entitled to give. The executor lives on this
        repository because the grant row is what the transaction is ABOUT; the
        alternative, a free function somewhere neutral, would be a fourth place
        that knows how these writes go together.
        """
        _quota_events_low_level_client().transact_write_items(TransactItems=items)


# A low-level (typed-value) DynamoDB client, constructed off the plain client
# rather than a resource's `.meta.client`, so the fragments' DynamoDB-JSON typed
# values pass through untouched. Cached per process, mirroring
# `tenant_budgets._budgets_low_level_client`.
_QUOTA_EVENTS_LL_CLIENT = None


def _quota_events_low_level_client():
    global _QUOTA_EVENTS_LL_CLIENT
    if _QUOTA_EVENTS_LL_CLIENT is None:
        import boto3

        from core.aws_pool import boto_config

        from .client import DYNAMODB_POOL_ENV
        _QUOTA_EVENTS_LL_CLIENT = boto3.client(
            "dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"),
            config=boto_config(DYNAMODB_POOL_ENV))
    return _QUOTA_EVENTS_LL_CLIENT


def _reset_quota_events_low_level_client() -> None:
    """Test hook: drop the cached low-level client so a new moto region takes
    effect (mirrors `tenant_budgets._reset_budgets_low_level_client`)."""
    global _QUOTA_EVENTS_LL_CLIENT
    _QUOTA_EVENTS_LL_CLIENT = None
