"""Step-0 performance spike: the TransactWriteItems floor as a function of the
number of items in the synchronous reserve transaction (Fable step-4/5 review).

WHY THIS EXISTS, BEFORE the correctness migration. The two-item migration's whole
performance premise is "removing the synchronous RESERVE ledger event (4 items ->
2/3) shrinks the p99 tail". The previous headroom benchmark showed the tail is
dominated by the DynamoDB TransactWriteItems FLOOR, not (only) contention — so the
projected "6,512 -> ~200 ms" was NOT met (measured 4,508 ms). Fable's lesson:
"measure the floor before claiming the model shrinks it." This spike measures the
floor at 4 / 3 / 2 items DIRECTLY, so we know whether the migration can even reach
p99 < 50 ms before investing the correctness work. It writes throwaway ledger rows
(correctness is intentionally NOT preserved here) into a bench namespace and
cleans up.

It reuses the REAL item builders (same item shapes/sizes as production — Fable:
"same item size, else the comparison is dirty") and composes them into 4/3/2-item
TransactWriteItems variants:

  * 4-item  = pool headroom ADD + HOLD Put + RESERVE event + IDEMP row (today).
  * 3-item  = pool headroom ADD + HOLD Put + IDEMP row (RESERVE event async'd; the
              step-5 external shape still keeping the idempotency row separate).
  * 2-item  = pool headroom ADD + HOLD Put (RESERVE async'd AND IDEMP folded into
              the HOLD deterministic key — the step-6 end state).

Per-txn attribution (Fable): every call records SDK retry count, the number of
TransactionCanceledException hits, and the cancellation reason breakdown, so the
tail decomposes into "contention (TransactionConflict)" vs "service-side floor".

Usage (on the same-region load-gen EC2, from backend/ so `mvp`/`dynamo` import):
    DYNAMODB_* env -> the target DynamoDB tables, AWS creds available,
    python -m bench.ledger_latency.bench_itemcount_spike \
        --tenant spike-01 --pool-microusd 100000000000 \
        --sequential 5000 --concurrent-iters 5000 --concurrency 16 \
        --out-dir /tmp/spike

Emits, per item-count x {c=1 floor, c=16}: p50/p90/p99/p99.9/max plus the retry /
TransactionConflict attribution. Cleans up the bench namespace unless --keep.
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

    return {
        "count": n, "p50": pct(50), "p90": pct(90), "p99": pct(99),
        "p99_9": pct(99.9), "max": round(s[-1], 3), "min": round(s[0], 3),
        "mean": round(sum(s) / n, 3),
    }


@dataclass
class _Attrib:
    """Per-phase attribution so the tail can be split into contention vs floor."""
    calls: int = 0
    errors: int = 0
    total_retries: int = 0
    txn_conflict_txns: int = 0        # txns that hit >=1 TransactionConflict
    reasons: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls, "errors": self.errors,
            "total_retries": self.total_retries,
            "txns_with_transaction_conflict": self.txn_conflict_txns,
            "cancellation_reasons": dict(self.reasons),
        }


def _build_items(pipeline_mods, tenant_id: str, period: str, amount: int, n_items: int):
    """Construct the TransactItems list for an `n_items`-item reserve variant,
    reusing the production builders so item shapes/sizes match. Returns
    (items, hold_id)."""
    budgets, ledger = pipeline_mods
    hold_id = uuid.uuid4().hex
    hold_expires_at = int(time.time()) + 3600
    key = f"spike-{hold_id}"

    from dynamo.tenant_budgets import hold_sk as _hsk
    hold_sk = _hsk(period, hold_expires_at, hold_id)

    pool_txn = budgets.reserve_txn_item(
        tenant_id=tenant_id, period=period, amount_microusd=amount)
    hold_txn = budgets.hold_put_txn_item(
        tenant_id=tenant_id, period=period, hold_id=hold_id,
        amount_microusd=amount, expires_at_epoch=hold_expires_at,
        source="external", description="itemcount-spike",
        payload_hash=key, run_id=hold_id, run_id_is_fallback=True)
    items = [pool_txn, hold_txn]

    if n_items >= 4:
        items.append(ledger.reserve_event_txn_item(
            tenant_id=tenant_id, period=period, hold_id=hold_id,
            reserved_delta_microusd=amount, run_id=hold_id, run_id_is_fallback=True,
            source="external", description="itemcount-spike"))
    if n_items >= 3:
        items.append(ledger.idemp_txn_item(
            tenant_id=tenant_id, period=period, idempotency_key=key,
            hold_id=hold_id, hold_sk=hold_sk, authorization_id=key,
            amount_microusd=amount, expires_at_epoch=hold_expires_at,
            capture_mode="amount", request_fingerprint=key))
    # n_items == 2 -> just [pool, hold]; the IDEMP row is folded into the HOLD's
    # deterministic key in the real step-6 end state, so a separate item is gone.
    return items, hold_id


# A bounded retry mirroring the app's reserve loop, but instrumented: it counts
# retries and TransactionConflict so the tail can be attributed. Mirrors
# mvp/_pipeline reserve semantics (retry on TransactionConflict/throttle only).
_MAX_RETRIES = 8


def _one_txn(client, token_fn, items, attrib: _Attrib) -> float:
    from botocore.exceptions import ClientError

    t0 = time.perf_counter()
    hit_conflict = False
    for attempt in range(_MAX_RETRIES):
        try:
            client.transact_write_items(TransactItems=items, ClientRequestToken=token_fn())
            dt = (time.perf_counter() - t0) * 1000.0
            attrib.calls += 1
            attrib.total_retries += attempt
            if hit_conflict:
                attrib.txn_conflict_txns += 1
            return dt
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            reasons = e.response.get("CancellationReasons") or []
            for r in reasons:
                rc = r.get("Code")
                if rc and rc != "None":
                    attrib.reasons[rc] += 1
            is_conflict = code in ("TransactionConflictException", "ThrottlingException",
                                   "ProvisionedThroughputExceededException") or any(
                r.get("Code") == "TransactionConflict" for r in reasons)
            if is_conflict and attempt < _MAX_RETRIES - 1:
                hit_conflict = True
                # full-jitter backoff, same family as the app
                import random
                time.sleep(min(0.05 * (2 ** attempt), 0.5) * random.random())
                continue
            attrib.errors += 1
            attrib.calls += 1
            return -1.0
    attrib.errors += 1
    attrib.calls += 1
    return -1.0


def _run_phase(client, mods, tenant_id, period, amount, n_items, count, concurrency):
    attrib = _Attrib()
    rows: list[float] = []

    def _task(_):
        items, _hid = _build_items(mods, tenant_id, period, amount, n_items)
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
    ap = argparse.ArgumentParser(prog="bench_itemcount_spike")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--pool-microusd", type=int, default=100_000_000_000)
    ap.add_argument("--sequential", type=int, default=5000)
    ap.add_argument("--concurrent-iters", type=int, default=5000)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--item-counts", default="4,3,2",
                    help="comma-separated item counts to sweep (default 4,3,2)")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)

    import boto3

    from dynamo.credit_ledger import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

    os.makedirs(args.out_dir, exist_ok=True)
    period = current_period()
    budgets = TenantBudgetsRepository()
    ledger = CreditLedgerRepository()
    mods = (budgets, ledger)
    client = boto3.client("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))

    item_counts = [int(x) for x in args.item_counts.split(",")]
    results: dict = {"metric": "ledger_itemcount_spike", "tenant": args.tenant,
                     "period": period, "by_item_count": {}}

    for n_items in item_counts:
        # A fresh pool per item-count so a lowered headroom from a prior sweep
        # never starves the next. A huge pool: we measure the write, not a 402.
        budgets.set_pool_limit(tenant_id=args.tenant, period=period,
                               pool_limit_microusd=args.pool_microusd, status="active")

        floor_rows, floor_attr = _run_phase(
            client, mods, args.tenant, period, args.amount_microusd, n_items,
            args.sequential, 1)
        conc_rows, conc_attr = _run_phase(
            client, mods, args.tenant, period, args.amount_microusd, n_items,
            args.concurrent_iters, args.concurrency)

        with open(os.path.join(args.out_dir, f"spike_{n_items}item.csv"), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["phase", "latency_ms"])
            for x in floor_rows:
                w.writerow(["floor_c1", x])
            for x in conc_rows:
                w.writerow([f"concurrent_c{args.concurrency}", x])

        results["by_item_count"][n_items] = {
            "floor_c1": {**_percentiles(floor_rows), "attribution": floor_attr.as_dict()},
            f"concurrent_c{args.concurrency}": {
                **_percentiles(conc_rows), "attribution": conc_attr.as_dict()},
        }
        print(f"[spike] {n_items}-item done: "
              f"floor p99={results['by_item_count'][n_items]['floor_c1'].get('p99')} "
              f"c{args.concurrency} p99="
              f"{results['by_item_count'][n_items][f'concurrent_c{args.concurrency}'].get('p99')}")

    print(json.dumps(results, indent=2))
    with open(os.path.join(args.out_dir, "spike_summary.json"), "w") as fh:
        json.dump(results, fh, indent=2)

    if not args.keep:
        _cleanup(args.tenant)
    return 0


def _cleanup(tenant_id: str) -> None:
    """Delete the bench tenant's budget/hold rows AND the throwaway ledger rows."""
    from boto3.dynamodb.conditions import Key

    from dynamo.client import credit_ledger_table_name
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    import boto3

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
        # ledger rows for this tenant/period live under pk TENANT#<id>#PERIOD#<p>
        ddb = boto3.resource("dynamodb", region_name=os.getenv("AWS_REGION", "us-east-1"))
        ltab = ddb.Table(credit_ledger_table_name())
        from dynamo.credit_ledger import ledger_pk
        pk = ledger_pk(tenant_id, current_period())
        lk = {"KeyConditionExpression": Key("pk").eq(pk)}
        while True:
            resp = ltab.query(**lk)
            with ltab.batch_writer() as bw:
                for it in resp.get("Items", []):
                    bw.delete_item(Key={"pk": it["pk"], "sk": it["sk"]})
                    n += 1
            lek = resp.get("LastEvaluatedKey")
            if not lek:
                break
            lk["ExclusiveStartKey"] = lek
        print(f"[cleanup] deleted {n} bench rows for {tenant_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[cleanup][warn] manual cleanup may be needed for {tenant_id} "
              f"({type(e).__name__}: {e})")


if __name__ == "__main__":
    raise SystemExit(main())
