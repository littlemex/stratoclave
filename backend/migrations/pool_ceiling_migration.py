"""Migrate the tenant pool row from the `sizing` mode to the ceiling rule.

The old row said WHICH MODE it was in (`sizing = "per_seat" | "fixed"`, or the
attribute absent). The new row says what the ceiling is MADE OF, and the mode
falls out of it:

    seat_term  = seat_count x seat_rate
    baseline   = manual_limit  if manual_limit is PRESENT  else seat_term
    pool_limit = baseline + coalesce(pool_granted, 0)

Every phase below is idempotent and safe to run against live traffic. None of
them changes any row's EFFECTIVE LIMIT: that is the property the whole migration
is organised around, and `--verify` is what checks it rather than asserts it.

FIVE ORDERED PHASES
-------------------
M1  Seed the stored seat rate on every pool row and record the rate in force.
    Changes no effective limit. Must be deployed everywhere before M2, because
    M2 DIVIDES by the rate this phase seeded.

M2  Backfill `seat_count` and `manual_limit_microusd`, as a CAS on the observed
    `pool_limit` (and `seat_count` where it already exists) -- M1's dual-writing
    deploy moves those concurrently, so a blind write would clobber a membership
    delta that landed in between.

      * a `per_seat` row becomes `seat_count = pool_limit / seat_rate` with
        `manual_limit` LEFT ABSENT. A non-integer quotient goes on the
        adjudication list and is NOT migrated: a rounded seat count is a
        plausible number standing in for a row nobody understands.
      * a `fixed` row, OR A ROW WITH NO `sizing` AT ALL, becomes
        `manual_limit = pool_limit` (including `0`, which is a figure meaning
        every request refused), with `seat_count` taken from a strongly
        consistent membership count.

    An absent `sizing` migrates as an OPERATOR FIGURE, not as a seat-tracked
    row. Tenant creation has written `per_seat` since the seat mechanism
    shipped, so an unlabelled row predates it and its figure was chosen when
    seats did not exist. Reading absence as seat-tracked would start those
    ceilings moving behind the operator's back.

M2b Compatibility window. Nothing to write: old-shape writers preserve the new
    attributes and new-shape writers honour both while `sizing` still exists.
    This phase REPORTS whether the fleet is in that state, row by row, which is
    the gate M3 needs.

M3  Cut over. Verifies the adjudication list is empty and repairs any row that
    still carries neither new attribute to `manual_limit = pool_limit` --
    FAIL-STALE, never fail-closed: a row nobody migrated keeps the ceiling it
    has rather than dropping to zero and refusing every request.

M4  Delete `sizing`. Gated on a clean reconciler pass over every row.

WHY THIS IS ONE-SHOT
--------------------
**No phase here may be re-run once grants exist.** M3's fail-stale read treats a
row carrying neither new attribute as `manual_limit = pool_limit`. Run against a
row that has since accumulated `pool_granted_microusd`, it would fold granted
money permanently into the operator's figure -- and at migration scale that is
every row at once. The rule that makes M3 safe before grants exist is exactly
what makes a re-run unsafe after. So every phase refuses outright on a table
where any row carries a granted amount, rather than leaving that as a sentence
in a document for somebody to have read.

Run (report first, then apply):
    python -m migrations.pool_ceiling_migration --verify
    python -m migrations.pool_ceiling_migration --phase m1 --dry-run
    python -m migrations.pool_ceiling_migration --phase m1 --apply
    ...
    python -m migrations.pool_ceiling_migration --recompute-seat-rate --apply
"""
from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any, Iterator, Optional

from boto3.dynamodb.conditions import Attr


class MigrationRefused(RuntimeError):
    """Raised when a precondition of a phase does not hold. Always a refusal to
    write, never a partial run: a migration that half-applies is worse than one
    that has not started, because the fleet is then in a state no phase
    describes."""


# ---------------------------------------------------------------------------
# Row iteration and classification
# ---------------------------------------------------------------------------
_SIZING_PER_SEAT = "per_seat"   # the value the old creation path wrote
_SIZING_FIXED = "fixed"         # the value an explicit operator set wrote


def _iter_budget_rows(table) -> Iterator[dict[str, Any]]:
    """Every `BUDGET#` item, paginated. HOLD and MARKER rows share the table and
    carry none of this; only the aggregate pool row is a ceiling."""
    kwargs: dict[str, Any] = {"FilterExpression": Attr("sk").begins_with("BUDGET#")}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _period_of(sk: str) -> str:
    """`BUDGET#<period>` -> `<period>`."""
    return sk.split("#", 1)[1] if "#" in sk else sk


# ---------------------------------------------------------------------------
# The one-shot guard
# ---------------------------------------------------------------------------
def assert_no_grants_present(table) -> None:
    """Refuse every phase on a table where any pool row carries a granted amount.

    This is the sentence "the migration is one-shot" made into a mechanism.
    M3 reads a row carrying neither new attribute as `manual_limit = pool_limit`,
    which is right while `pool_limit` is only ever a baseline and catastrophic
    once it also contains granted money: the grant would be folded into the
    operator's figure permanently, on every such row at once.
    """
    from dynamo.tenant_budgets import POOL_GRANTED_ATTR

    for item in _iter_budget_rows(table):
        if POOL_GRANTED_ATTR in item:
            raise MigrationRefused(
                f"{item.get('tenant_id')}/{item.get('sk')} carries "
                f"{POOL_GRANTED_ATTR}, so grants exist on this table and no "
                f"migration phase may run: M3's fail-stale read would fold "
                f"granted money into the operator's figure permanently. The "
                f"migration is one-shot and its window has closed."
            )


# ---------------------------------------------------------------------------
# The property every phase preserves, checked rather than asserted
# ---------------------------------------------------------------------------
def effective_limit_before(item: dict[str, Any]) -> int:
    """What the OLD code admitted against: the stored `pool_limit_microusd`. The
    mode never entered the admission arithmetic -- it only decided whether a
    membership change moved the figure -- so the stored number IS the old
    effective limit, for all three row states."""
    return int(item.get("pool_limit_microusd", 0))


def effective_limit_after(item: dict[str, Any]) -> int:
    """What the rule computes for the same row."""
    from dynamo.tenant_budgets import expected_pool_limit_microusd

    return expected_pool_limit_microusd(item)


def verify(table) -> dict[str, Any]:
    """Compare every row's effective limit before and after the rule, and report
    each difference. Read-only, and the whole point of the migration: a phase
    that moved a ceiling would show up here as a row whose two figures disagree.

    Also reports any attribute the closed-world declaration does not classify,
    because a migration is exactly when an undeclared attribute appears.
    """
    from dynamo.tenant_budgets import unclassified_pool_attributes

    rows = 0
    moved: list[dict[str, Any]] = []
    undeclared: dict[str, list[str]] = {}
    for item in _iter_budget_rows(table):
        rows += 1
        before, after = effective_limit_before(item), effective_limit_after(item)
        if before != after:
            moved.append({
                "tenant_id": str(item.get("tenant_id")),
                "sk": str(item.get("sk")),
                "before_microusd": before,
                "after_microusd": after,
                "delta_microusd": after - before,
            })
        extra = unclassified_pool_attributes(item)
        if extra:
            undeclared[f"{item.get('tenant_id')}/{item.get('sk')}"] = sorted(extra)
    summary = {
        "rows": rows,
        "effective_limit_moved": len(moved),
        "moved_detail": moved[:50],
        "undeclared_attributes": undeclared,
        "clean": not moved and not undeclared,
    }
    print(f"[verify] {summary}")
    return summary


# ---------------------------------------------------------------------------
# M1 -- seed the stored seat rate
# ---------------------------------------------------------------------------
def phase_m1_add_attributes(*, apply: bool) -> dict[str, Any]:
    """Seed `seat_rate_microusd` on every pool row, and record the rate in force.

    The rate has to exist BEFORE M2, because M2 divides `pool_limit` by it. Taking
    the live environment value at M2 time instead would mean an operator who
    changed `STRATOCLAVE_SEAT_MONTHLY_USD` between the two phases converts every
    seat-scaled row to a wrong seat count that looks entirely well-formed. Seeding
    it here makes the division reproducible across a re-run and a rollback, and a
    mid-migration change of the environment value then hits the boot-time refusal,
    which is the behaviour that refusal was written for.

    Changes no effective limit: it writes one attribute that nothing reads until
    M2, and the rate it writes is the rate the ceilings were already computed at.
    """
    from dynamo.tenant_budgets import (
        SEAT_RATE_ATTR,
        TenantBudgetsRepository,
        seat_rate_microusd,
    )

    repo = TenantBudgetsRepository()
    table = repo._table
    assert_no_grants_present(table)
    rate = seat_rate_microusd()
    in_force = repo.rate_in_force_microusd()
    if in_force is not None and in_force != rate:
        raise MigrationRefused(
            f"the rate in force is {in_force} micro-USD/seat/month but this "
            f"process is configured for {rate}. M1 seeds the rate the existing "
            f"ceilings were computed at; run --recompute-seat-rate to change it.")

    scanned = seeded = already = 0
    for item in _iter_budget_rows(table):
        scanned += 1
        if SEAT_RATE_ATTR in item:
            already += 1
            continue
        if apply:
            table.update_item(
                Key={"tenant_id": item["tenant_id"], "sk": item["sk"]},
                UpdateExpression="SET seat_rate_microusd = :r, updated_at = :now",
                # Only if still absent, so a concurrent seed is never doubled and
                # a row that already carries a DIFFERENT rate is left for the
                # reconciler to flag rather than silently overwritten.
                ConditionExpression="attribute_not_exists(seat_rate_microusd)",
                ExpressionAttributeValues={":r": Decimal(rate), ":now": _now()},
            )
        seeded += 1
    if apply:
        repo.record_rate_in_force(rate_microusd=rate)
    summary = {"phase": "m1", "scanned": scanned, "seeded": seeded,
               "already_had_rate": already, "rate_microusd": rate, "applied": apply}
    print(f"[m1] {summary}")
    return summary


# ---------------------------------------------------------------------------
# M2 -- backfill seat_count and manual_limit as a CAS
# ---------------------------------------------------------------------------
def classify_for_backfill(item: dict[str, Any], *, rate: int) -> dict[str, Any]:
    """Decide what M2 would write for one row, WITHOUT writing.

    Returns a dict with `action` in {`seat_tracked`, `operator_figure`,
    `adjudicate`, `done`} and the values it would write.
    """
    from dynamo.tenant_budgets import MANUAL_LIMIT_ATTR, SEAT_COUNT_ATTR

    limit = int(item.get("pool_limit_microusd", 0))
    sizing = item.get("sizing")
    has_manual = MANUAL_LIMIT_ATTR in item
    has_seats = SEAT_COUNT_ATTR in item
    if has_manual or (has_seats and sizing == _SIZING_PER_SEAT):
        return {"action": "done"}
    if sizing == _SIZING_PER_SEAT:
        if rate <= 0:
            return {"action": "adjudicate",
                    "reason": f"seat rate is {rate}, so the quotient is undefined"}
        if limit % rate != 0:
            # A rounded seat count is a plausible number standing in for a row
            # nobody understands, which is the one substitution this pipeline
            # refuses. The row is listed for a human and left exactly as it is.
            return {"action": "adjudicate",
                    "reason": (f"pool_limit {limit} is not a whole multiple of the "
                               f"seat rate {rate} ({limit / rate:.4f} seats)")}
        return {"action": "seat_tracked", "seat_count": limit // rate}
    # `fixed`, or NO sizing attribute at all: an operator's figure either way.
    return {"action": "operator_figure", "manual_limit_microusd": limit}


def phase_m2_backfill(*, apply: bool) -> dict[str, Any]:
    """Backfill the two new attributes, as a CAS on what was observed.

    The CAS is on `pool_limit_microusd` (and on `seat_count` where the row already
    carries one) because M1's deploy has membership changes dual-writing those
    concurrently: a blind write would clobber a delta that landed between the read
    and the write, and the symptom would be a ceiling one seat out with nothing
    recording why.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository
    from dynamo.user_tenants import UserTenantsRepository

    repo = TenantBudgetsRepository()
    table = repo._table
    assert_no_grants_present(table)
    rate = repo.rate_in_force_microusd()
    if rate is None:
        raise MigrationRefused(
            "no rate in force is recorded: M1 has not run, and M2 divides by the "
            "rate M1 seeds. Taking the environment's value here instead would "
            "convert every seat-scaled row at whatever the rate happens to be now.")

    counts = UserTenantsRepository().active_membership_counts()
    scanned = seat_tracked = operator_figure = done = lost_cas = 0
    adjudication: list[dict[str, Any]] = []
    for item in _iter_budget_rows(table):
        scanned += 1
        tid, sk = item["tenant_id"], item["sk"]
        plan = classify_for_backfill(item, rate=int(rate))
        if plan["action"] == "done":
            done += 1
            continue
        if plan["action"] == "adjudicate":
            adjudication.append({"tenant_id": str(tid), "sk": str(sk),
                                 "reason": plan["reason"],
                                 "pool_limit_microusd": int(
                                     item.get("pool_limit_microusd", 0))})
            continue

        observed_limit = Decimal(int(item.get("pool_limit_microusd", 0)))
        cond = ["pool_limit_microusd = :obs_limit"]
        values: dict[str, Any] = {":obs_limit": observed_limit, ":now": _now()}
        if "seat_count" in item:
            cond.append("seat_count = :obs_seats")
            values[":obs_seats"] = Decimal(int(item["seat_count"]))

        if plan["action"] == "seat_tracked":
            # `manual_limit` is LEFT ABSENT: absence is the sentinel for "follow
            # the seats", so there is nothing to write for it.
            expr = "SET seat_count = :seats, updated_at = :now"
            values[":seats"] = Decimal(int(plan["seat_count"]))
            seat_tracked += 1
        else:
            # The seat count comes from the membership count, not from the figure:
            # the figure is the operator's and says nothing about how many people
            # are there. Written so the row can later report that its entitlement
            # has outgrown the figure.
            expr = ("SET manual_limit_microusd = :manual, seat_count = :seats, "
                    "updated_at = :now")
            values[":manual"] = Decimal(int(plan["manual_limit_microusd"]))
            values[":seats"] = Decimal(int(counts.get(str(tid), 0)))
            operator_figure += 1
        if apply:
            from botocore.exceptions import ClientError
            try:
                table.update_item(
                    Key={"tenant_id": tid, "sk": sk},
                    UpdateExpression=expr,
                    ConditionExpression=" AND ".join(cond),
                    ExpressionAttributeValues=values,
                )
            except ClientError as e:
                if e.response.get("Error", {}).get("Code") \
                        != "ConditionalCheckFailedException":
                    raise
                # A membership delta or an operator set landed under us. Leave the
                # row for the next pass rather than recomputing from a stale read:
                # the phase is idempotent and re-running is free.
                lost_cas += 1

    summary = {"phase": "m2", "scanned": scanned, "seat_tracked": seat_tracked,
               "operator_figure": operator_figure, "already_migrated": done,
               "lost_cas_retry_next_pass": lost_cas,
               "adjudication": adjudication, "applied": apply}
    print(f"[m2] {summary}")
    return summary


def phase_m2b_report() -> dict[str, Any]:
    """Report whether the whole fleet is in the compatibility state M3 requires:
    every row carries at least one of the new attributes, and the adjudication
    list is empty. Writes nothing -- the compatibility itself is a property of the
    deployed code, and this is the evidence that the data caught up with it."""
    from dynamo.tenant_budgets import (
        MANUAL_LIMIT_ATTR,
        SEAT_COUNT_ATTR,
        TenantBudgetsRepository,
    )

    repo = TenantBudgetsRepository()
    rate = repo.rate_in_force_microusd() or 0
    rows = migrated = unmigrated = 0
    pending: list[str] = []
    adjudication: list[dict[str, Any]] = []
    for item in _iter_budget_rows(repo._table):
        rows += 1
        plan = classify_for_backfill(item, rate=int(rate))
        if plan["action"] == "adjudicate":
            adjudication.append({"tenant_id": str(item.get("tenant_id")),
                                 "sk": str(item.get("sk")),
                                 "reason": plan["reason"]})
            continue
        if MANUAL_LIMIT_ATTR in item or SEAT_COUNT_ATTR in item:
            migrated += 1
        else:
            unmigrated += 1
            pending.append(f"{item.get('tenant_id')}/{item.get('sk')}")
    summary = {"phase": "m2b", "rows": rows, "migrated": migrated,
               "unmigrated": unmigrated, "unmigrated_detail": pending[:50],
               "adjudication": adjudication,
               "ready_for_m3": unmigrated == 0 and not adjudication}
    print(f"[m2b] {summary}")
    return summary


# ---------------------------------------------------------------------------
# M3 -- cut over
# ---------------------------------------------------------------------------
def phase_m3_cutover(*, apply: bool) -> dict[str, Any]:
    """Refuse while anything is unadjudicated, then repair the stragglers.

    A row that reaches here carrying NEITHER new attribute is read as
    `manual_limit = pool_limit`. FAIL-STALE, never fail-closed: the row keeps the
    ceiling it has. The fail-closed alternative -- treating it as seat-tracked
    with no seat count, hence a ceiling of zero -- would refuse every request for
    a tenant whose only problem is that a backfill missed it.

    This is also the read that makes the migration one-shot. Once `pool_limit`
    can contain granted money, folding it into the operator's figure would make
    the grant permanent; `assert_no_grants_present` is why that cannot happen by
    accident.
    """
    from dynamo.tenant_budgets import (
        MANUAL_LIMIT_ATTR,
        SEAT_COUNT_ATTR,
        TenantBudgetsRepository,
    )

    repo = TenantBudgetsRepository()
    table = repo._table
    assert_no_grants_present(table)
    pre = phase_m2b_report()
    if pre["adjudication"]:
        raise MigrationRefused(
            f"{len(pre['adjudication'])} row(s) are on the adjudication list and "
            f"the cutover refuses while it is non-empty: each one is a ceiling "
            f"whose composition nobody has established, and guessing produces a "
            f"well-formed wrong number. Detail: {pre['adjudication'][:10]}")

    repaired = 0
    for item in _iter_budget_rows(table):
        if MANUAL_LIMIT_ATTR in item or SEAT_COUNT_ATTR in item:
            continue
        limit = int(item.get("pool_limit_microusd", 0))
        if apply:
            table.update_item(
                Key={"tenant_id": item["tenant_id"], "sk": item["sk"]},
                UpdateExpression=(
                    "SET manual_limit_microusd = :manual, updated_at = :now"),
                # Still absent, and the limit is still what we read: a row a
                # concurrent backfill has since fixed must not be overwritten with
                # a figure derived from a stale total.
                ConditionExpression=(
                    "attribute_not_exists(manual_limit_microusd) AND "
                    "pool_limit_microusd = :obs"),
                ExpressionAttributeValues={
                    ":manual": Decimal(limit), ":obs": Decimal(limit),
                    ":now": _now()},
            )
        repaired += 1
        print(f"[m3] fail-stale repair {item.get('tenant_id')}/{item.get('sk')}: "
              f"manual_limit <- {limit}")
    summary = {"phase": "m3", "fail_stale_repaired": repaired, "applied": apply}
    print(f"[m3] {summary}")
    return summary


# ---------------------------------------------------------------------------
# M4 -- delete `sizing`
# ---------------------------------------------------------------------------
def phase_m4_drop_sizing(*, apply: bool, reconciler_pass_clean: bool = False
                         ) -> dict[str, Any]:
    """Remove `sizing`. Gated on a clean full reconciler pass over every row.

    The gate is a parameter rather than something this function decides, because
    the reconciler pass is the evidence and evidence has to come from outside the
    thing it licenses. `--reconciler-pass-clean` is the operator asserting they
    have that pass; the pass itself is
    `mvp.observability.quota_reconciler.reconcile_all`.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository

    repo = TenantBudgetsRepository()
    table = repo._table
    assert_no_grants_present(table)
    if apply and not reconciler_pass_clean:
        raise MigrationRefused(
            "M4 needs one clean full post-M3 reconciler pass over EVERY row first. "
            "Run `python -m mvp.observability.quota_reconciler` and pass "
            "--reconciler-pass-clean once it reports no findings. Dropping "
            "`sizing` is the point of no return for reading the old shape.")
    dropped = 0
    for item in _iter_budget_rows(table):
        if "sizing" not in item:
            continue
        if apply:
            table.update_item(
                Key={"tenant_id": item["tenant_id"], "sk": item["sk"]},
                UpdateExpression="REMOVE sizing SET updated_at = :now",
                ExpressionAttributeValues={":now": _now()},
            )
        dropped += 1
    summary = {"phase": "m4", "sizing_dropped": dropped, "applied": apply}
    print(f"[m4] {summary}")
    return summary


# ---------------------------------------------------------------------------
# The rate change, which is the only legal way the rate in force moves
# ---------------------------------------------------------------------------
def recompute_seat_tracked_rows(*, apply: bool) -> dict[str, Any]:
    """Change the rate in force and recompute every seat-tracked row at it.

    This is the door the boot-time refusal points at. A seat-tracked row's
    ceiling is `seat_count x rate`, so a rate change moves it -- deliberately,
    once, here -- and the row's stored rate moves with it so the ceiling stays
    reproducible. A row holding an operator's figure is NOT touched: the figure is
    a number a person chose and the rate has nothing to say about it.

    Requires `STRATOCLAVE_SEAT_RATE_MIGRATION`, because the process running this
    is the one process that is allowed to disagree with the stored rate.
    """
    from dynamo.tenant_budgets import (
        MANUAL_LIMIT_ATTR,
        SEAT_RATE_ATTR,
        SEAT_RATE_MIGRATION_ENV,
        TenantBudgetsRepository,
        seat_rate_microusd,
        seat_rate_migration_allowed,
    )

    if not seat_rate_migration_allowed():
        raise MigrationRefused(
            f"set {SEAT_RATE_MIGRATION_ENV}=1 to recompute the seat rate. Without "
            f"it this process would be refusing to boot against the very rows it "
            f"is here to change, which is the refusal working correctly.")
    repo = TenantBudgetsRepository()
    table = repo._table
    assert_no_grants_present(table)
    new_rate = seat_rate_microusd()
    old_rate = repo.rate_in_force_microusd()

    scanned = recomputed = untouched_manual = 0
    moved: list[dict[str, Any]] = []
    for item in _iter_budget_rows(table):
        scanned += 1
        if MANUAL_LIMIT_ATTR in item:
            untouched_manual += 1
            continue
        seats = int(item.get("seat_count", 0))
        old_limit = int(item.get("pool_limit_microusd", 0))
        new_limit = seats * new_rate
        delta = new_limit - old_limit
        if delta == 0 and int(item.get(SEAT_RATE_ATTR, 0)) == new_rate:
            continue
        if apply:
            table.update_item(
                Key={"tenant_id": item["tenant_id"], "sk": item["sk"]},
                UpdateExpression=(
                    "SET seat_rate_microusd = :r, updated_at = :now "
                    "ADD pool_limit_microusd :d, pool_headroom_microusd :d"),
                # The ceiling moves as an ADD so a live reserve's own headroom ADD
                # composes with it, and the CAS is on the two figures the delta was
                # computed from.
                ConditionExpression=(
                    "pool_limit_microusd = :obs_limit AND seat_count = :obs_seats"),
                ExpressionAttributeValues={
                    ":r": Decimal(new_rate), ":d": Decimal(delta),
                    ":obs_limit": Decimal(old_limit),
                    ":obs_seats": Decimal(seats), ":now": _now()},
            )
        recomputed += 1
        moved.append({"tenant_id": str(item.get("tenant_id")),
                      "sk": str(item.get("sk")), "seats": seats,
                      "from_microusd": old_limit, "to_microusd": new_limit})
    if apply:
        repo.record_rate_in_force(rate_microusd=new_rate)
    summary = {"phase": "recompute-seat-rate", "scanned": scanned,
               "recomputed": recomputed, "untouched_operator_figures":
               untouched_manual, "old_rate_microusd": old_rate,
               "new_rate_microusd": new_rate, "moved_detail": moved[:50],
               "applied": apply}
    print(f"[recompute] {summary}")
    return summary


# ---------------------------------------------------------------------------
def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


_PHASES = {
    "m1": lambda apply: phase_m1_add_attributes(apply=apply),
    "m2": lambda apply: phase_m2_backfill(apply=apply),
    "m2b": lambda apply: phase_m2b_report(),
    "m3": lambda apply: phase_m3_cutover(apply=apply),
}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="pool_ceiling_migration")
    ap.add_argument("--phase", choices=sorted(_PHASES) + ["m4"],
                    help="which ordered phase to run")
    ap.add_argument("--verify", action="store_true",
                    help="report every row whose effective limit would move "
                         "(read-only; the property the migration preserves)")
    ap.add_argument("--recompute-seat-rate", action="store_true",
                    help="change the rate in force and recompute seat-tracked rows")
    ap.add_argument("--reconciler-pass-clean", action="store_true",
                    help="assert a clean full post-M3 reconciler pass (M4's gate)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", help="report, write nothing")
    g.add_argument("--apply", action="store_true", help="write")
    args = ap.parse_args(argv)

    if args.verify:
        from dynamo.tenant_budgets import TenantBudgetsRepository

        summary = verify(TenantBudgetsRepository()._table)
        return 0 if summary["clean"] else 1
    if args.recompute_seat_rate:
        recompute_seat_tracked_rows(apply=bool(args.apply))
        return 0
    if not args.phase:
        ap.error("give --phase, --verify or --recompute-seat-rate")
    if args.phase == "m4":
        phase_m4_drop_sizing(apply=bool(args.apply),
                             reconciler_pass_clean=bool(args.reconciler_pass_clean))
        return 0
    _PHASES[args.phase](bool(args.apply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
