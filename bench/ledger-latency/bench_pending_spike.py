"""Step-0b performance spike: the PENDING protocol vs the transaction, measured.

The item-count spike (bench_itemcount_spike.py) proved the c=16 p99 tail is
100% SDK-backoff accumulation on `TransactionConflict` — a function of running a
TRANSACTION on a hot pool row, independent of item count. Fable's redirect: the
next measured step is the PENDING protocol, because a SINGLE conditional
`UpdateItem` (no transaction) has NO `TransactionConflict` failure mode — the
leader replica serializes concurrent single-item writes in a queue instead of
cancelling them, so the retry-storm-then-backoff mechanism that produces the
~1,190 ms c=16 tail cannot occur. It only fails on a genuine
`ConditionalCheckFailedException` (business rejection = pool exhausted).

This spike measures, on the SAME hot pool row, real DynamoDB, per-transaction
attribution:

  * `single_update` — ONE conditional UpdateItem on the pool row (the exact
    headroom ADD + condition the reserve_txn_item builds, issued directly, NOT in
    a transaction). This isolates the money-gate write.
  * `pending_e2e`   — the full 3-write PENDING protocol: Put HOLD `PENDING`
    (uncontended, unique key) -> conditional UpdateItem on the pool (the ONLY
    contended write, non-transactional) -> Update HOLD `ACTIVE`. Three sequential
    round-trips; the prediction to TEST is that its p99 (not p50) beats the
    transaction because the contended middle write cannot enter a conflict storm.

Concurrency sweep c=1 / c=16 / c=64 (Fable: find the single-item write ceiling
knee, ~1,000 writes/s per partition). Pool seeded huge so ConditionalCheckFailed
(business rejection) is zero and only latency is measured.

Usage (same-region load-gen EC2, from backend/ so `dynamo` imports resolve):
    DYNAMODB_TENANT_BUDGETS_TABLE=<budgets-table> AWS_REGION=<region> \
    python3 bench/ledger-latency/bench_pending_spike.py \
        --tenant pending-01 --pool-microusd 100000000000 \
        --sequential 3000 --concurrent-iters 3000 --out-dir /tmp/pending

Emits, per mode x concurrency: p50/p90/p99/p99.9/max + retry/CCF attribution.
Cleans up the bench namespace unless --keep.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field


def _percentiles(samples_ms: list[float]) -> dict:
    s = sorted(x for x in samples_ms if x >= 0)
    n = len(s)
    if not n:
        return {"count": 0}

    def pct(p: float) -> float:
        idx = min(n - 1, int(round(p / 100.0 * n + 0.5)) - 1)
        return round(s[max(0, idx)], 3)

    return {"count": n, "p50": pct(50), "p90": pct(90), "p99": pct(99),
            "p99_9": pct(99.9), "max": round(s[-1], 3), "min": round(s[0], 3),
            "mean": round(sum(s) / n, 3)}


@dataclass
class _Attrib:
    calls: int = 0
    errors: int = 0
    ccf: int = 0                       # ConditionalCheckFailed (business reject)
    total_retries: int = 0
    reasons: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {"calls": self.calls, "errors": self.errors,
                "conditional_check_failed": self.ccf,
                "total_retries": self.total_retries,
                "error_codes": dict(self.reasons)}


_MAX_RETRIES = 8


def _pool_update_args(table_name, tenant_id, period, amount, budget_sk):
    """The exact non-transactional form of the reserve pool item: a conditional
    ADD to headroom + reserved mirror, guarded on headroom coverage. Same
    expression the transactional reserve_txn_item uses, issued as a bare
    UpdateItem (no transaction => no TransactionConflict failure mode)."""
    now = str(int(time.time() * 1000))
    return dict(
        TableName=table_name,
        Key={"tenant_id": {"S": tenant_id}, "sk": {"S": budget_sk(period)}},
        UpdateExpression=("ADD pool_headroom_microusd :neg, pool_reserved_microusd :amt "
                          "SET updated_at = :now"),
        ConditionExpression=("attribute_exists(pool_headroom_microusd) AND #st = :active AND "
                             "pool_headroom_microusd >= :amt"),
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":neg": {"N": str(-int(amount))}, ":amt": {"N": str(int(amount))},
            ":active": {"S": "active"}, ":now": {"S": now}},
    )


def _retrying_write(client, fn, attrib: _Attrib) -> bool:
    """Run a single write with the app's retry family (retry on
    throttle/conflict; CCF is a terminal business reject, NOT retried). Returns
    True on success. Records attribution."""
    from botocore.exceptions import ClientError
    import random

    for attempt in range(_MAX_RETRIES):
        try:
            fn()
            attrib.total_retries += attempt
            return True
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            attrib.reasons[code] += 1
            if code == "ConditionalCheckFailedException":
                attrib.ccf += 1
                return False           # business rejection, terminal (not an error)
            if code in ("ThrottlingException", "ProvisionedThroughputExceededException",
                        "TransactionConflictException") and attempt < _MAX_RETRIES - 1:
                time.sleep(min(0.05 * (2 ** attempt), 0.5) * random.random())
                continue
            attrib.errors += 1
            return False
    attrib.errors += 1
    return False


def _one_single_update(client, budgets_table, tenant_id, period, amount, budget_sk, attrib):
    t0 = time.perf_counter()
    ok = _retrying_write(
        client, lambda: client.update_item(
            **_pool_update_args(budgets_table, tenant_id, period, amount, budget_sk)),
        attrib)
    attrib.calls += 1
    return (time.perf_counter() - t0) * 1000.0 if ok else -1.0


def _one_pending_e2e(client, budgets_table, tenant_id, period, amount, budget_sk, hold_sk, attrib):
    """PENDING protocol: Put HOLD PENDING -> conditional UpdateItem pool -> HOLD ACTIVE."""
    hold_id = uuid.uuid4().hex
    exp = int(time.time()) + 3600
    sk = hold_sk(period, exp, hold_id)
    t0 = time.perf_counter()

    def put_pending():
        client.put_item(
            TableName=budgets_table,
            Item={"tenant_id": {"S": tenant_id}, "sk": {"S": sk},
                  "hold_id": {"S": hold_id}, "period": {"S": period},
                  "amount_microusd": {"N": str(int(amount))},
                  "expires_at": {"N": str(exp)}, "status": {"S": "PENDING"},
                  "created_at": {"S": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}},
            ConditionExpression="attribute_not_exists(sk)")

    def activate():
        client.update_item(
            TableName=budgets_table,
            Key={"tenant_id": {"S": tenant_id}, "sk": {"S": sk}},
            UpdateExpression="SET #s = :active",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":active": {"S": "ACTIVE"}})

    if not _retrying_write(client, put_pending, attrib):
        attrib.calls += 1
        return -1.0
    if not _retrying_write(
            client, lambda: client.update_item(
                **_pool_update_args(budgets_table, tenant_id, period, amount, budget_sk)),
            attrib):
        attrib.calls += 1
        return -1.0
    if not _retrying_write(client, activate, attrib):
        attrib.calls += 1
        return -1.0
    attrib.calls += 1
    return (time.perf_counter() - t0) * 1000.0


def _run(mode_fn, count, concurrency):
    attrib = _Attrib()
    rows: list[float] = []
    if concurrency == 1:
        for _ in range(count):
            rows.append(mode_fn(attrib))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(mode_fn, attrib) for _ in range(count)]
            for f in as_completed(futs):
                rows.append(f.result())
    return rows, attrib


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_pending_spike")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--pool-microusd", type=int, default=100_000_000_000)
    ap.add_argument("--sequential", type=int, default=3000)
    ap.add_argument("--concurrent-iters", type=int, default=3000)
    ap.add_argument("--concurrencies", default="16,64")
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    import boto3

    from dynamo.tenant_budgets import (
        TenantBudgetsRepository, budget_sk, current_period,
    )
    from dynamo.tenant_budgets import hold_sk as _hold_sk

    os.makedirs(args.out_dir, exist_ok=True)
    period = current_period()
    budgets = TenantBudgetsRepository()
    budgets_table = budgets._name
    client = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))

    concurrencies = [int(x) for x in args.concurrencies.split(",")]
    results: dict = {"metric": "ledger_pending_spike", "tenant": args.tenant,
                     "period": period, "by_mode": {}}

    def _reseed():
        # A huge pool so the money gate never rejects; we measure latency only.
        budgets.set_pool_limit(tenant_id=args.tenant, period=period,
                               pool_limit_microusd=args.pool_microusd, status="active")

    modes = {
        "single_update": lambda attrib: _one_single_update(
            client, budgets_table, args.tenant, period, args.amount_microusd, budget_sk, attrib),
        "pending_e2e": lambda attrib: _one_pending_e2e(
            client, budgets_table, args.tenant, period, args.amount_microusd,
            budget_sk, _hold_sk, attrib),
    }

    for mode_name, mode_fn in modes.items():
        results["by_mode"][mode_name] = {}
        _reseed()
        floor_rows, floor_attr = _run(mode_fn, args.sequential, 1)
        results["by_mode"][mode_name]["floor_c1"] = {
            **_percentiles(floor_rows), "attribution": floor_attr.as_dict()}
        _write_csv(args.out_dir, mode_name, "floor_c1", floor_rows)
        for c in concurrencies:
            _reseed()
            rows, attr = _run(mode_fn, args.concurrent_iters, c)
            results["by_mode"][mode_name][f"c{c}"] = {
                **_percentiles(rows), "attribution": attr.as_dict()}
            _write_csv(args.out_dir, mode_name, f"c{c}", rows)
            print(f"[pending] {mode_name} c{c}: p99="
                  f"{results['by_mode'][mode_name][f'c{c}'].get('p99')}ms "
                  f"errors={attr.errors} ccf={attr.ccf}")

    print(json.dumps(results, indent=2))
    with open(os.path.join(args.out_dir, "pending_summary.json"), "w") as fh:
        json.dump(results, fh, indent=2)

    if not args.keep:
        _cleanup(args.tenant)
    return 0


def _write_csv(out_dir, mode, phase, rows):
    path = os.path.join(out_dir, f"pending_{mode}_{phase}.csv")
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["phase", "latency_ms"])
        for x in rows:
            w.writerow([phase, x])


def _cleanup(tenant_id: str) -> None:
    from boto3.dynamodb.conditions import Key

    from dynamo.tenant_budgets import TenantBudgetsRepository
    try:
        repo = TenantBudgetsRepository()
        table = repo._table
        n = 0
        kwargs = {"KeyConditionExpression": Key("tenant_id").eq(tenant_id)}
        while True:
            resp = table.query(**kwargs)
            with table.batch_writer() as bw:
                for it in resp.get("Items", []):
                    bw.delete_item(Key={"tenant_id": tenant_id, "sk": it["sk"]})
                    n += 1
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        print(f"[cleanup] deleted {n} bench rows for {tenant_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup][warn] manual cleanup may be needed for {tenant_id} "
              f"({type(e).__name__}: {e})")


if __name__ == "__main__":
    raise SystemExit(main())
