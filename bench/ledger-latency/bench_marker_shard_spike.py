"""Step-0c performance spike: does the MARKER cost the 88ms win, and does SHARDING
get c=16 under the 50ms target? (Fable review: measure before claiming.)

Two questions the PENDING marker redesign left open (docs/design/
pending-protocol.md, vsr-savings-certificate.md):

  1. The money-safe PENDING commit now writes a per-hold marker `applied.<hold_id>`
     ATOMICALLY with the pool debit in ONE UpdateItem. step-0b measured the pool
     UpdateItem WITHOUT the marker (c=16 p99 88ms). Does the marker — which grows
     the pool item by one map entry per live hold, and enlarges the write — cost
     that win? This bench mirrors the PRODUCTION expression exactly.
  2. c=16 p99 (marker or not) was 88ms, just over the 50ms target — Fable said the
     single hot row is near its physical floor and only SHARDING gets under it.
     This bench measures a sharded pool (N sub-counter rows; each reserve picks a
     shard by hash, so the per-row concurrency is ~c/N) at N=1/2/4/8.

Throwaway: writes marker/counter rows into a bench namespace, cleans up. Run on
the same-region load-gen EC2 against the live tables.

Usage:
    python3 bench/ledger-latency/bench_marker_shard_spike.py \
        --tenant mshard-01 --pool-microusd 100000000000 \
        --sequential 3000 --concurrent-iters 3000 --concurrency 16 \
        --shards 1,2,4,8 --out-dir /tmp/mshard
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


def _pct(samples, p):
    s = sorted(x for x in samples if x >= 0)
    n = len(s)
    if not n:
        return None
    idx = min(n - 1, int(round(p / 100.0 * n + 0.5)) - 1)
    return round(s[max(0, idx)], 3)


def _summary(rows):
    return {"count": len([r for r in rows if r >= 0]),
            "p50": _pct(rows, 50), "p90": _pct(rows, 90), "p99": _pct(rows, 99),
            "p99_9": _pct(rows, 99.9), "max": round(max([r for r in rows if r >= 0], default=0), 3)}


@dataclass
class _Attrib:
    calls: int = 0
    errors: int = 0
    ccf: int = 0
    retries: int = 0
    reasons: Counter = field(default_factory=Counter)

    def as_dict(self):
        return {"calls": self.calls, "errors": self.errors, "ccf": self.ccf,
                "retries": self.retries, "reasons": dict(self.reasons)}


_MAX_RETRIES = 8


def _marker_update_args(table, tenant_id, sk_val, hold_id, amount):
    """PRODUCTION-faithful marker commit: ADD headroom/reserved AND SET
    applied.<hold_id> in the SAME UpdateItem, guarded by headroom + marker-absent.
    Mirrors dynamo.tenant_budgets.pool_reserve_update exactly."""
    now = str(int(time.time() * 1000))
    return dict(
        TableName=table,
        Key={"tenant_id": {"S": tenant_id}, "sk": {"S": sk_val}},
        UpdateExpression=("ADD pool_headroom_microusd :neg, pool_reserved_microusd :amt "
                          "SET updated_at = :now, applied.#hid = :amt"),
        ConditionExpression=("attribute_exists(pool_headroom_microusd) AND #st = :active AND "
                             "pool_headroom_microusd >= :amt AND attribute_not_exists(applied.#hid)"),
        ExpressionAttributeNames={"#st": "status", "#hid": hold_id},
        ExpressionAttributeValues={":neg": {"N": str(-int(amount))},
                                   ":amt": {"N": str(int(amount))},
                                   ":active": {"S": "active"}, ":now": {"S": now}},
    )


def _do(client, args_fn, attrib):
    from botocore.exceptions import ClientError
    import random
    t0 = time.perf_counter()
    for attempt in range(_MAX_RETRIES):
        try:
            client.update_item(**args_fn())
            attrib.calls += 1
            attrib.retries += attempt
            return (time.perf_counter() - t0) * 1000.0
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            attrib.reasons[code] += 1
            if code == "ConditionalCheckFailedException":
                # headroom-exhaust OR marker-present. Fresh hold_id per call means
                # marker-absent always holds, so a CCF here is genuine exhaustion.
                attrib.ccf += 1
                attrib.calls += 1
                return -1.0
            if code in ("ThrottlingException", "ProvisionedThroughputExceededException") \
                    and attempt < _MAX_RETRIES - 1:
                time.sleep(min(0.05 * (2 ** attempt), 0.5) * random.random())
                continue
            attrib.errors += 1
            attrib.calls += 1
            return -1.0
    attrib.errors += 1
    attrib.calls += 1
    return -1.0


def _run(fn, count, concurrency):
    attrib = _Attrib()
    rows = []
    if concurrency == 1:
        for _ in range(count):
            rows.append(fn(attrib))
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(fn, attrib) for _ in range(count)]
            for f in as_completed(futs):
                rows.append(f.result())
    return rows, attrib


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_marker_shard_spike")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--pool-microusd", type=int, default=100_000_000_000)
    ap.add_argument("--sequential", type=int, default=3000)
    ap.add_argument("--concurrent-iters", type=int, default=3000)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--shards", default="1,2,4,8")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    import boto3
    from boto3.dynamodb.conditions import Key

    from dynamo.tenant_budgets import TenantBudgetsRepository, budget_sk, current_period

    os.makedirs(args.out_dir, exist_ok=True)
    period = current_period()
    budgets = TenantBudgetsRepository()
    table = budgets._name
    res = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    rtable = res.Table(table)
    client = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
    shards = [int(x) for x in args.shards.split(",")]
    per_shard = args.pool_microusd // max(shards)   # ensure each shard row has room

    def _seed_shard_rows(n):
        # N sub-counter rows: sk = BUDGET#<period>#shardK, each seeded with an
        # applied map + a share of the pool. The single-row case (n=1) reuses the
        # real budget row.
        if n == 1:
            budgets.set_pool_limit(tenant_id=args.tenant, period=period,
                                   pool_limit_microusd=args.pool_microusd, status="active")
            # ensure the applied map exists on the real row.
            budgets.ensure_applied_map(tenant_id=args.tenant, period=period)
            return [budget_sk(period)]
        sks = []
        for k in range(n):
            sk = f"{budget_sk(period)}#shard{k}"
            rtable.put_item(Item={
                "tenant_id": args.tenant, "sk": sk, "status": "active",
                "pool_headroom_microusd": per_shard, "pool_reserved_microusd": 0,
                "applied": {}})
            sks.append(sk)
        return sks

    def _cleanup_shard_rows():
        resp = rtable.query(KeyConditionExpression=Key("tenant_id").eq(args.tenant))
        n = 0
        with rtable.batch_writer() as bw:
            for it in resp.get("Items", []):
                bw.delete_item(Key={"tenant_id": args.tenant, "sk": it["sk"]})
                n += 1
        return n

    results = {"metric": "marker_shard_spike", "tenant": args.tenant, "by_shards": {}}
    for n in shards:
        _cleanup_shard_rows()
        sks = _seed_shard_rows(n)

        def _fn(attrib, _sks=sks):
            hid = uuid.uuid4().hex
            sk = _sks[hash(hid) % len(_sks)]     # pick a shard by hold id hash
            return _do(client,
                       lambda: _marker_update_args(table, args.tenant, sk, hid, args.amount_microusd),
                       attrib)

        floor, fattr = _run(_fn, args.sequential, 1)
        conc, cattr = _run(_fn, args.concurrent_iters, args.concurrency)
        with open(os.path.join(args.out_dir, f"shard{n}.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["phase", "ms"])
            for x in floor:
                w.writerow(["floor_c1", x])
            for x in conc:
                w.writerow([f"c{args.concurrency}", x])
        results["by_shards"][n] = {
            "floor_c1": {**_summary(floor), "attribution": fattr.as_dict()},
            f"c{args.concurrency}": {**_summary(conc), "attribution": cattr.as_dict()},
            "per_row_concurrency_est": round(args.concurrency / n, 2),
        }
        print(f"[mshard] N={n}: floor p99={results['by_shards'][n]['floor_c1']['p99']} "
              f"c{args.concurrency} p99={results['by_shards'][n][f'c{args.concurrency}']['p99']} "
              f"(~c/N={round(args.concurrency/n,1)})")

    print(json.dumps(results, indent=2))
    with open(os.path.join(args.out_dir, "marker_shard_summary.json"), "w") as fh:
        json.dump(results, fh, indent=2)
    if not args.keep:
        print(f"[cleanup] deleted {_cleanup_shard_rows()} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
