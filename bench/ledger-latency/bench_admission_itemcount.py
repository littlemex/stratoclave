"""B1: does one more conditional Update in the INLINE admission transaction move the
p99 into a different regime?

WHY THIS EXISTS ALONGSIDE `bench_itemcount_spike.py`

That bench answers a different question and its published figures depend on its arms
meaning what they mean. Its arms are the **external-authorize** shape — pool headroom,
hold, RESERVE ledger event, and an IDEMP row — and `docs/benchmarks/ledger-latency.md`
annotates the resulting numbers as such. Redefining those arms in place would silently
change what a published figure refers to.

The transaction a per-user ceiling is added to is the **inline admission** path, and it is
composed differently (`backend/mvp/_pipeline.py:3547`): `[user_txn, pool_txn, hold_txn]`,
then the per-model quota lines, then the RESERVE ledger event. It carries no IDEMP item at
all. Same count at four items, different content.

The second difference is the one that decides whether this measurement is worth taking. The
item a per-user dollar ceiling adds is a **user-scoped** quota Update
(`backend/mvp/routing/quota.py`, keyed `TENANT#<t>#USER#<u>`), and the per-user token debit
already in the transaction is keyed per user too. A benchmark whose writers are different
users leaves both of those rows uncontended, so it would measure the new item with the one
mechanism that can make it expensive switched off, and would report a comfortable number
for the wrong reason. So every writer here shares **one user** as well as one pool row.

ARMS (all against one hot pool row and one hot user)

  * 3 items = pool headroom + hold + RESERVE ledger event        (control)
  * 4 items = per-user token debit + the above                   (N, production today)
  * 5 items = a user-scoped quota Update + the above             (N + 1, with the ceiling)

The 3-item control is not decoration: it is what makes the 4-to-5 delta interpretable,
because it supplies a same-session, same-partition, same-client measurement of what one more
item of this shape costs. Comparing against a figure from another month and another table is
what the control exists to avoid.

The instrumented single-transaction call, the retry accounting and the percentile helper are
imported from `bench_itemcount_spike` rather than reimplemented. That code decides how a
retry is counted and how a cancellation is attributed, and two implementations of that would
diverge in exactly the statistic both benches report.

Usage (on a load generator in the SAME REGION as the tables; a cross-region client adds tens
of milliseconds of variance to the statistic under test):

    python -m bench.ledger_latency.bench_admission_itemcount \\
        --tenant b1-01 --user b1-user --concurrency 1,8,32 \\
        --iters 3000 --out-dir /tmp/b1

Thresholds for reading the output are pre-registered and are NOT in this file, deliberately:
a benchmark that also owns its own pass criteria can be adjusted until it passes.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bench_itemcount_spike import _Attrib, _one_txn, _percentiles  # noqa: E402

# Big enough that no arm is ever refused: this measures the cost of a write that succeeds.
# A run whose tail is partly 402s is measuring admission control, not item count.
POOL_MICROUSD = 100_000_000_000
USER_TOTAL_CREDIT = 10 ** 15
QUOTA_LIMIT_MICROUSD = 10 ** 14


def _build_inline_items(budgets, ledger, users, quota, *, tenant_id: str, user_id: str,
                        period: str, amount: int, n_items: int, expected_total: int):
    """The inline admission transaction at `n_items`, from the production builders.

    Order matters and is production's: user debit, pool, hold, quota lines, ledger event
    last. `_pipeline` relies on those fixed indices when it parses a cancellation, and a
    benchmark that reorders them would measure a transaction DynamoDB may treat differently
    with respect to which item cancels first.
    """
    hold_id = uuid.uuid4().hex
    hold_expires_at = int(time.time()) + 3600

    items = []
    if n_items >= 4:
        items.append(users.reserve_txn_item(
            user_id=user_id, tenant_id=tenant_id, tokens=amount,
            expected_total=expected_total))
    items.append(budgets.reserve_txn_item(
        tenant_id=tenant_id, period=period, amount_microusd=amount))
    items.append(budgets.hold_put_txn_item(
        tenant_id=tenant_id, period=period, hold_id=hold_id,
        amount_microusd=amount, expires_at_epoch=hold_expires_at,
        source="inline", description="b1-admission-itemcount",
        run_id=hold_id, run_id_is_fallback=True,
        model_id="b1-model", reserved_tokens=amount, hold_user_id=user_id))
    if n_items >= 5:
        # The ceiling's item, built by the production builder with a user limit and no
        # tenant limit, which is exactly the shape a user-scoped dollar wall produces.
        items.extend(quota.build_reserve_txn_items(
            tenant_id, user_id, "b1-model", period, amount,
            tenant_limit=None, user_limit=QUOTA_LIMIT_MICROUSD))
    items.append(ledger.reserve_event_txn_item(
        tenant_id=tenant_id, period=period, hold_id=hold_id,
        reserved_delta_microusd=amount, run_id=hold_id, run_id_is_fallback=True,
        source="inline", description="b1-admission-itemcount"))
    # The whole measurement is a comparison BETWEEN item counts, so an arm that quietly
    # builds a different number than it claims does not fail — it reports a comfortable
    # delta for the wrong reason. A builder change that drops or doubles an item stops the
    # run here instead.
    if len(items) != n_items:
        raise AssertionError(
            f"the {n_items}-item arm built {len(items)} items: "
            f"{[next(iter(i)) + ':' + i[next(iter(i))]['TableName'] for i in items]}")
    return items, hold_id


def _run_phase(client, builders, *, tenant_id, user_id, period, amount, n_items, count,
               concurrency, expected_total):
    attrib = _Attrib()
    rows: list[float] = []
    budgets, ledger, users, quota = builders

    def _task(_):
        items, _hid = _build_inline_items(
            budgets, ledger, users, quota, tenant_id=tenant_id, user_id=user_id,
            period=period, amount=amount, n_items=n_items, expected_total=expected_total)
        return _one_txn(client, lambda: uuid.uuid4().hex, items, attrib)

    if concurrency == 1:
        for i in range(count):
            rows.append(_task(i))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(_task, i) for i in range(count)]
            for f in as_completed(futs):
                rows.append(f.result())
    return rows, attrib


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_admission_itemcount")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--user", required=True,
                    help="ONE user id shared by every writer; see the module docstring")
    ap.add_argument("--iters", type=int, default=3000)
    ap.add_argument("--concurrency", default="1,8,32",
                    help="comma-separated writer counts (default 1,8,32)")
    ap.add_argument("--item-counts", default="3,4,5")
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args(argv)

    import boto3

    from dynamo.credit_ledger import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository
    from mvp.routing import quota as quota_mod
    from pool_fixture import seed_verified_pool

    os.makedirs(args.out_dir, exist_ok=True)
    period = current_period()
    budgets, ledger, users = (TenantBudgetsRepository(), CreditLedgerRepository(),
                              UserTenantsRepository())
    builders = (budgets, ledger, users, quota_mod)
    region = os.getenv("AWS_REGION") or "us-east-1"
    client = boto3.client("dynamodb", region_name=region)

    membership = users.ensure(user_id=args.user, tenant_id=args.tenant, role="user",
                              total_credit=USER_TOTAL_CREDIT)
    expected_total = int(membership["total_credit"])

    concurrencies = [int(x) for x in args.concurrency.split(",")]
    item_counts = [int(x) for x in args.item_counts.split(",")]
    results: dict = {
        "metric": "b1_admission_itemcount",
        "region": region,
        "tenant": args.tenant, "user": args.user, "period": period,
        "iters_per_arm": args.iters,
        "arms": "inline admission: 3=pool+hold+ledger, 4=+user debit, 5=+user-scoped quota",
        "by_item_count": {},
    }

    for n_items in item_counts:
        results["by_item_count"][str(n_items)] = {}
        for c in concurrencies:
            # A fresh pool per arm so a headroom lowered by the previous arm never turns
            # this one's tail into refusals. The fixture's identity check is what proves
            # the seeded row is a row the product could have produced.
            seed_verified_pool(budgets, tenant_id=args.tenant, period=period,
                               manual_limit_microusd=POOL_MICROUSD, status="active")
            rows, attrib = _run_phase(
                client, builders, tenant_id=args.tenant, user_id=args.user, period=period,
                amount=args.amount_microusd, n_items=n_items, count=args.iters,
                concurrency=c, expected_total=expected_total)
            key = f"c{c}"
            results["by_item_count"][str(n_items)][key] = {
                **_percentiles(rows), "attribution": attrib.as_dict()}
            with open(os.path.join(args.out_dir, f"b1_{n_items}item_c{c}.csv"),
                      "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["latency_ms"])
                for x in rows:
                    w.writerow([x])
            got = results["by_item_count"][str(n_items)][key]
            print(f"[b1] {n_items} items, c={c}: p50={got.get('p50')} p99={got.get('p99')} "
                  f"conflicts={got['attribution'].get('txns_with_transaction_conflict')}/"
                  f"{got['attribution'].get('calls')} errors={got['attribution'].get('errors')}",
                  flush=True)

    out = os.path.join(args.out_dir, "b1_admission_itemcount.json")
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)
    print(f"[b1] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
