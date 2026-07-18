"""B-layer micro-benchmark: the isolated cost of the ledger's synchronous
TransactWriteItems, with HTTP / ALB / network removed.

This is the *decomposition* evidence for the headline number produced by
bench_live.py — NOT the headline itself. It answers "of the end-to-end authorize
latency, how much is the DynamoDB ledger round-trip?" by calling
``reserve_external_authorization`` directly against real DynamoDB, in-process,
from a same-region host. Run it on the load-gen EC2 (same region as the tables)
so the DynamoDB round-trip is the regional path, not a WAN hop.

Design (per the benchmark plan): sequential 5,000 + concurrent-16 5,000, against
a bench-only tenant namespace, cleaned up on exit. Emits p50/p90/p99/p99.9/max.

Usage (on the load-gen EC2, from the backend/ dir so `mvp` imports resolve):
    DYNAMODB_* env set to the live scverify tables, AWS creds available,
    python -m bench.ledger_latency.bench_micro --tenant bench-micro-01 \
        --pool-microusd 100000000 --sequential 5000 --concurrent-iters 5000 \
        --concurrency 16 --out /tmp/micro.csv

It provisions the bench tenant's pool via the same repositories the app uses,
runs the two phases, writes a raw-latency CSV, prints the percentile summary,
and (unless --keep) removes the bench tenant's rows.
"""
from __future__ import annotations

import argparse
import csv
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed


def _percentiles(samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {}
    s = sorted(samples_ms)
    n = len(s)

    def pct(p: float) -> float:
        # nearest-rank; for p99 over >=10k samples this is a well-supported stat.
        idx = min(n - 1, int(round(p / 100.0 * n + 0.5)) - 1)
        return round(s[max(0, idx)], 3)

    return {
        "count": n,
        "p50": pct(50), "p90": pct(90), "p99": pct(99),
        "p99_9": pct(99.9), "max": round(s[-1], 3),
        "min": round(s[0], 3),
        "mean": round(sum(s) / n, 3),
    }


def _one_reserve(pipeline, tenant_id: str, amount: int) -> float:
    """Time a single reserve_external_authorization (the ledger TransactWriteItems).
    Returns the wall-clock ms. A unique idempotency key per call so every call is
    a fresh hold (never a replay)."""
    from mvp.billing_authorize import encode_authorization_id  # local import

    key = f"bench-{uuid.uuid4().hex}"
    t0 = time.perf_counter()
    pipeline.reserve_external_authorization(
        tenant_id=tenant_id,
        amount_microusd=amount,
        idempotency_key=key,
        request_fingerprint=key,
        authorization_id_factory=lambda hold_id, period, hold_sk: encode_authorization_id(
            hold_id=hold_id, period=period, hold_sk=hold_sk),
        ttl_seconds=3600,
        description="ledger-latency micro-bench",
    )
    return (time.perf_counter() - t0) * 1000.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_micro")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--pool-microusd", type=int, default=100_000_000_000)
    ap.add_argument("--sequential", type=int, default=5000)
    ap.add_argument("--concurrent-iters", type=int, default=5000)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", action="store_true", help="do not clean up the bench tenant")
    args = ap.parse_args(argv)

    from mvp import _pipeline as pipeline
    import json

    # Provision the bench tenant's pool (best-effort; the operator seeds the
    # pool row out-of-band if the repo API differs — kept generic here).
    _seed_pool(args.tenant, args.pool_microusd)

    rows: list[tuple[str, float]] = []

    # Phase 1: sequential.
    seq_ms: list[float] = []
    for _ in range(args.sequential):
        ms = _one_reserve(pipeline, args.tenant, args.amount_microusd)
        seq_ms.append(ms)
        rows.append(("sequential", ms))

    # Phase 2: concurrent.
    conc_ms: list[float] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(_one_reserve, pipeline, args.tenant, args.amount_microusd)
                for _ in range(args.concurrent_iters)]
        for f in as_completed(futs):
            try:
                ms = f.result()
                conc_ms.append(ms)
                rows.append((f"concurrent-{args.concurrency}", ms))
            except Exception as e:  # noqa: BLE001 — a failed reserve is a data point too.
                rows.append((f"concurrent-{args.concurrency}", -1.0))
                print(f"[warn] reserve failed: {type(e).__name__}: {e}")

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["phase", "latency_ms"])
        w.writerows(rows)

    summary = {
        "sequential": _percentiles(seq_ms),
        f"concurrent_{args.concurrency}": _percentiles(conc_ms),
    }
    print(json.dumps({"metric": "ledger_micro_bench", "tenant": args.tenant,
                      "summary": summary}, indent=2))

    if not args.keep:
        _cleanup(args.tenant)
    return 0


def _seed_pool(tenant_id: str, pool_microusd: int) -> None:
    """Ensure the bench tenant has a dollar pool with `pool_microusd` for the
    current period, via the same TenantBudgets repository the app uses. Re-runnable
    (set_pool_limit preserves running counters). A big pool so the bench never
    hits pool exhaustion and measures the ledger write, not a 402 path."""
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    repo = TenantBudgetsRepository()
    period = current_period()
    repo.set_pool_limit(tenant_id=tenant_id, period=period,
                        pool_limit_microusd=pool_microusd, status="active")
    print(f"[seed] pool for {tenant_id} period {period}: {pool_microusd} micro-USD")


def _cleanup(tenant_id: str) -> None:
    """Delete the bench tenant's budget + hold rows for the current period. The
    TenantBudgets table is PK tenant_id / SK sk, so a Query + BatchWrite delete of
    every row under this bench tenant is exhaustive and touches no other tenant."""
    try:
        from boto3.dynamodb.conditions import Key
        from dynamo.tenant_budgets import TenantBudgetsRepository
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
        print(f"[cleanup] deleted {n} rows for bench tenant {tenant_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup][warn] manual cleanup needed for {tenant_id} "
              f"({type(e).__name__}: {e})")


if __name__ == "__main__":
    raise SystemExit(main())
