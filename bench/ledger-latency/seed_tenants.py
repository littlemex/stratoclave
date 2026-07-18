"""Seed / tear down the bench tenants' dollar pools.

The A-layer authorize benchmark reserves from a tenant pool, so every bench
tenant needs a pool for the current period big enough that the run never hits
exhaustion (we are measuring the ledger write, not the 402 path). This uses the
same TenantBudgets repository the app uses, so the rows are byte-identical to a
real pool. Bench tenant ids are namespaced (`bench-t00`, `bench-t01`, …) so
cleanup can delete them exhaustively without touching a real tenant.

Usage (on the load-gen EC2, backend/ on PYTHONPATH, live DYNAMODB_* env set):
    python seed_tenants.py --prefix bench-t --count 32 --pool-microusd 100000000000
    python seed_tenants.py --prefix bench-t --count 32 --delete   # teardown
"""
from __future__ import annotations

import argparse


def tenant_ids(prefix: str, count: int) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(count)]


def seed(prefix: str, count: int, pool_microusd: int) -> None:
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    repo = TenantBudgetsRepository()
    period = current_period()
    for tid in tenant_ids(prefix, count):
        repo.set_pool_limit(tenant_id=tid, period=period,
                            pool_limit_microusd=pool_microusd, status="active")
    print(f"[seed] {count} tenants ({prefix}00..{prefix}{count-1:02d}) "
          f"pool={pool_microusd} micro-USD for period {period}")


def delete(prefix: str, count: int) -> None:
    from boto3.dynamodb.conditions import Key
    from dynamo.tenant_budgets import TenantBudgetsRepository
    repo = TenantBudgetsRepository()
    table = repo._table
    total = 0
    for tid in tenant_ids(prefix, count):
        kwargs = {"KeyConditionExpression": Key("tenant_id").eq(tid)}
        while True:
            resp = table.query(**kwargs)
            with table.batch_writer() as bw:
                for it in resp.get("Items", []):
                    bw.delete_item(Key={"tenant_id": tid, "sk": it["sk"]})
                    total += 1
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
    print(f"[delete] removed {total} rows across {count} bench tenants ({prefix}*)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="seed_tenants")
    ap.add_argument("--prefix", default="bench-t")
    ap.add_argument("--count", type=int, default=32)
    ap.add_argument("--pool-microusd", type=int, default=100_000_000_000)
    ap.add_argument("--delete", action="store_true", help="tear down instead of seed")
    args = ap.parse_args(argv)
    if args.delete:
        delete(args.prefix, args.count)
    else:
        seed(args.prefix, args.count, args.pool_microusd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
