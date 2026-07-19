"""A-layer benchmark (the headline): end-to-end POST /api/mvp/billing/authorize
latency from a same-region client over a persistent keep-alive connection.

This produces the number Stratoclave publishes: "in-region client, persistent
connection, N tenants, concurrency C, sustained T — p99 = X ms". The interval
measured is deliberately defined and stated in the report: from sending the
request on an already-established keep-alive connection to receiving the final
response byte. It INCLUDES ALB + Fargate app + DynamoDB round-trip; it EXCLUDES
TLS/connection setup (a real proxy client reuses connections) and CloudFront /
WAN (unrelated to ledger speed). Run this on the load-gen EC2 in the same region
as the ALB, hitting the ALB directly (not CloudFront).

Modes (per the benchmark plan):
  * A-1 (headline): many tenants, fixed concurrency, closed-loop, sustained.
  * A-2 (contention limit): a SINGLE tenant, concurrency stepped 1 -> 8 -> 32,
    to expose the single-pool-row CAS contention curve honestly.
  * A-3 (fault): same as A-1; the operator issues one `ecs stop-task` mid-run
    (this script just keeps driving load and records the latency/error timeline).

Every request uses a unique Idempotency-Key (a fresh hold each time, never a
replay). The caller supplies a bearer token (an admin/tenant token) and the base
URL (the ALB DNS, http/https). Tenants must be pre-seeded with a pool (use
seed_tenants.py). Raw per-request latency + status + timestamp go to a CSV so the
report can compute percentiles and the fault-window timeline offline.

Usage (on the load-gen EC2):
    python bench_live.py --base-url https://<alb-dns> --token-file /tmp/tok \
        --mode a1 --tenants bench-t00,bench-t01,... --concurrency 32 \
        --duration-s 300 --warmup-s 60 --out /tmp/a1.csv
"""
from __future__ import annotations

import argparse
import csv
import itertools
import threading
import time
import uuid

import httpx


def _percentiles(samples_ms):
    if not samples_ms:
        return {}
    s = sorted(samples_ms)
    n = len(s)

    def pct(p):
        idx = min(n - 1, int(round(p / 100.0 * n + 0.5)) - 1)
        return round(s[max(0, idx)], 3)

    return {"count": n, "p50": pct(50), "p90": pct(90), "p99": pct(99),
            "p99_9": pct(99.9), "max": round(s[-1], 3), "min": round(s[0], 3),
            "mean": round(sum(s) / n, 3)}


def _worker(base_url, token, tenants_cycle, stop_at, warmup_until, rows, lock,
            amount_microusd, timeout_s):
    """Closed-loop worker: reuses ONE keep-alive client, fires authorize as fast
    as it can until stop_at, records (ts, tenant, latency_ms, status, phase)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # http2=False: authorize is a small POST; HTTP/1.1 keep-alive is the realistic
    # proxy-client transport and keeps the measured interval unambiguous.
    with httpx.Client(base_url=base_url, headers=headers, timeout=timeout_s,
                      limits=httpx.Limits(max_keepalive_connections=1, max_connections=1)) as client:
        # prime the connection once (excluded from measurement by warmup).
        while time.time() < stop_at:
            tenant = next(tenants_cycle)
            body = {"amount_microusd": amount_microusd,
                    "description": "ledger-latency bench"}
            key = f"bench-{uuid.uuid4().hex}"
            t0 = time.perf_counter()
            ts = time.time()
            try:
                r = client.post("/api/mvp/billing/authorize", json=body,
                                headers={"Idempotency-Key": key})
                ms = (time.perf_counter() - t0) * 1000.0
                status = r.status_code
            except Exception as e:  # noqa: BLE001 — a transport error is a data point.
                ms = (time.perf_counter() - t0) * 1000.0
                status = -1
                _ = e
            phase = "warmup" if ts < warmup_until else "measure"
            with lock:
                rows.append((round(ts, 3), tenant, round(ms, 3), status, phase))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_live")
    ap.add_argument("--base-url", required=True, help="ALB base URL (in-region)")
    ap.add_argument("--token-file", required=True, help="file holding the bearer access token")
    ap.add_argument("--mode", choices=["a1", "a2"], default="a1")
    ap.add_argument("--tenants", required=True, help="comma-separated tenant ids")
    ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--duration-s", type=int, default=300)
    ap.add_argument("--warmup-s", type=int, default=60)
    ap.add_argument("--amount-microusd", type=int, default=1)
    ap.add_argument("--timeout-s", type=float, default=10.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    token = open(args.token_file).read().strip()
    tenants = [t for t in args.tenants.split(",") if t]
    if args.mode == "a2" and len(tenants) != 1:
        print("[warn] a2 (contention) is meant for a SINGLE tenant; using the first")
        tenants = tenants[:1]

    rows: list[tuple] = []
    lock = threading.Lock()
    start = time.time()
    warmup_until = start + args.warmup_s
    stop_at = start + args.warmup_s + args.duration_s
    # a shared, thread-safe tenant cycler so load spreads round-robin across tenants
    tenants_cycle = _ThreadSafeCycle(tenants)

    threads = [threading.Thread(target=_worker, args=(
        args.base_url, token, tenants_cycle, stop_at, warmup_until, rows, lock,
        args.amount_microusd, args.timeout_s)) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "tenant", "latency_ms", "status", "phase"])
        w.writerows(rows)

    import json
    measure = [r[2] for r in rows if r[4] == "measure" and r[3] == 200]
    errors = [r for r in rows if r[4] == "measure" and r[3] != 200]
    n_measure = sum(1 for r in rows if r[4] == "measure")
    print(json.dumps({
        "metric": "ledger_live_bench", "mode": args.mode,
        "concurrency": args.concurrency, "tenants": len(tenants),
        "duration_s": args.duration_s,
        "measured_requests": n_measure,
        "error_count": len(errors),
        "error_rate": round(len(errors) / n_measure, 5) if n_measure else None,
        "latency_ms": _percentiles(measure),
    }, indent=2))
    return 0


class _ThreadSafeCycle:
    def __init__(self, items):
        self._cycle = itertools.cycle(items)
        self._lock = threading.Lock()

    def __next__(self):
        with self._lock:
            return next(self._cycle)


if __name__ == "__main__":
    raise SystemExit(main())
