"""Backfill / reconcile `pool_headroom_microusd` on TenantBudgets pool rows.

Migration step 1 for the hot-path ledger design (docs/design/ledger-hot-path.md).
The reserve gate now reads/writes a single `pool_headroom_microusd` counter and
its condition is `attribute_exists(pool_headroom_microusd) AND headroom >= amt`.
A pool row created before this change has no such attribute, so a reserve
against it would (correctly, fail-closed) be rejected until it is backfilled
with `headroom = limit - reserved - settled`.

This scans the TenantBudgets table for BUDGET# rows and, for each, delegates to
`TenantBudgetsRepository.reconcile_headroom`, which recomputes the invariant from
the always-correct reserved/settled mirrors and writes it under a race-safe CAS.
Two properties matter (Fable review finding 2):

  * VALUE-repair, not presence-seed. During a rolling deploy a new-code settle
    can fire on a not-yet-backfilled row, whose unconditional `ADD pool_headroom`
    CREATES the attribute at a WRONG value (`reserved - actual`). A presence-gated
    backfill would see the attribute present and skip that row forever. reconcile
    keys on the VALUE, so it REPAIRS such a row instead of skipping it.
  * Race-safe. reconcile's CAS is `attribute_not_exists(pool_headroom) OR
    pool_headroom = :observed`, so a reserve/settle that moved headroom between
    the read and the write is never clobbered (it re-reads; the drift may be
    gone). No "quiet window" requirement — the job is safe to run live and to
    re-run any number of times.

No raw counter write is introduced here: reconcile lives in the reviewed repo
layer and never mutates pool_reserved/pool_settled (write-discipline / A2 clean).

Run (read-only preview first, then apply):
    python -m migrations.backfill_pool_headroom --dry-run
    python -m migrations.backfill_pool_headroom --apply
"""
from __future__ import annotations

import argparse

from boto3.dynamodb.conditions import Attr


def _iter_budget_rows(table):
    """Yield every BUDGET# item (the pool ceiling rows), paginated. HOLD# rows
    are skipped — only the aggregate pool row carries the counters."""
    kwargs: dict = {"FilterExpression": Attr("sk").begins_with("BUDGET#")}
    while True:
        resp = table.scan(**kwargs)
        for item in resp.get("Items", []):
            yield item
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def _period_of(sk: str) -> str:
    """BUDGET#<period> -> <period>."""
    return sk.split("#", 1)[1] if "#" in sk else sk


def _classify(item) -> tuple[str, int]:
    """Return (state, target_headroom) for reporting, WITHOUT writing.

    state in {ok, missing, drifted, negative}: `ok` = already at the invariant;
    `missing` = no headroom attribute (legacy row); `drifted` = headroom present
    but != invariant (e.g. a mixed-window settle created a wrong value);
    `negative` = the invariant itself is < 0 (over-reserved row — reported, still
    repaired to the true negative so the gate keeps refusing, never left corrupt).
    """
    limit = int(item.get("pool_limit_microusd", 0))
    reserved = int(item.get("pool_reserved_microusd", 0))
    settled = int(item.get("pool_settled_microusd", 0))
    target = limit - reserved - settled
    if target < 0:
        return "negative", target
    if "pool_headroom_microusd" not in item:
        return "missing", target
    if int(item["pool_headroom_microusd"]) != target:
        return "drifted", target
    return "ok", target


def backfill(*, apply: bool) -> dict:
    from dynamo.tenant_budgets import TenantBudgetsRepository

    repo = TenantBudgetsRepository()
    table = repo._table
    scanned = 0
    already = 0        # state == ok
    updated = 0        # missing + drifted that we (would) repair
    drifted = 0        # subset of updated whose headroom was present but wrong
    skipped_neg = 0    # target < 0 (over-reserved), reported and repaired to true value
    for item in _iter_budget_rows(table):
        scanned += 1
        state, target = _classify(item)
        tid = item.get("tenant_id")
        period = _period_of(str(item.get("sk")))
        if state == "ok":
            already += 1
            continue
        if state == "negative":
            skipped_neg += 1
            print(f"[warn] {tid}/{item.get('sk')}: limit-reserved-settled = "
                  f"{target} < 0 (over-reserved); reconciling to the true "
                  f"negative so the gate keeps refusing — investigate the row")
        if state == "drifted":
            drifted += 1
        if apply:
            # reconcile_headroom repairs to the invariant race-safely, whether
            # the attribute is absent, wrong, or (rarely) negative.
            repo.reconcile_headroom(tid, period)
        updated += 1
        print(f"[{'apply' if apply else 'dry-run'}] {tid}/{item.get('sk')}: "
              f"state={state} headroom -> {target}")
    summary = {"scanned": scanned, "already_at_invariant": already,
               "reconciled": updated, "of_which_drifted": drifted,
               "negative_invariant": skipped_neg, "applied": apply}
    print(f"[summary] {summary}")
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="backfill_pool_headroom")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="report what would change")
    g.add_argument("--apply", action="store_true", help="reconcile the headroom counter")
    args = ap.parse_args(argv)
    backfill(apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
