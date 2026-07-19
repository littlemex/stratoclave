"""Measure the load-generator -> ALB route overhead (TCP connect x1000).

Run this on the load-gen host BEFORE the authorize benchmark so the reported
p99<50ms figures can be read against a stated route overhead — the interval
definition in docs/benchmarks/ledger-latency.md cites this number so the
VPC-crossing hop is disclosed, not hidden. If p99 here is more than a few ms the
route has something unexpected and the benchmark's "in-region" premise is void.

    python route_overhead.py <alb-dns>
"""
from __future__ import annotations

import socket
import sys
import time


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    port = 80
    xs: list[float] = []
    for _ in range(1000):
        t = time.perf_counter()
        s = socket.create_connection((host, port), timeout=5)
        s.close()
        xs.append((time.perf_counter() - t) * 1000)
    xs.sort()
    n = len(xs)
    print(
        "TCP_connect_ms n=%d p50=%.3f p90=%.3f p99=%.3f p99_9=%.3f max=%.3f min=%.3f mean=%.3f"
        % (n, xs[n // 2], xs[int(n * 0.9)], xs[int(n * 0.99)],
           xs[min(n - 1, int(n * 0.999))], xs[-1], xs[0], sum(xs) / n)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
