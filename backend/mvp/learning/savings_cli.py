"""Internal ops CLI: the VSR Savings Certificate (the core-weapon report).

    python -m mvp.learning.savings_cli --tenant <id> --day YYYYMMDD [--json]

Emits, for a (tenant, day), the counterfactual saving of having FOLLOWED the
VSR's routing advice — priced at ledger precision over the requests' real token
counts (see mvp.learning.savings + docs/design/vsr-savings-certificate.md). The
number litellm cannot produce: gross saving, escalation loss SUBTRACTED (never
hidden), net, and honest coverage (how much of the traffic could even be priced).

HONESTY: `net = gross - escalation` can be NEGATIVE. That is a feature — a
certificate that can show a loss is one a buyer trusts to show a gain. Quality
parity is NOT asserted here; `quality.measured=false` until a tenant eval fills
it, and no saving should be externally CLAIMED before that.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import savings as sv


def _fmt_usd(microusd: int) -> str:
    neg = int(microusd) < 0
    a = abs(int(microusd))
    return f"{'-' if neg else ''}${a // 1_000_000}.{a % 1_000_000:06d}"


NOTICE = (
    "NOTE: savings are COUNTERFACTUAL — 'if the tenant had followed the VSR', "
    "priced from the versioned rate table over each request's REAL billed tokens. "
    "net = gross - escalation_loss and CAN be negative (a workload the VSR routed "
    "cheap but that escalated dearer). Only the 'counterfactual' class is in the "
    "savings base; every other request is named in class_counts, never counted as "
    "0 saving silently. QUALITY IS NOT MEASURED HERE — do not externally claim a "
    "saving until a tenant-defined eval confirms quality parity for the routed "
    "traffic."
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="savings_cli")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--day", required=True, help="YYYYMMDD (UTC)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--detail", action="store_true",
                    help="also list per-request counterfactual rows")
    args = ap.parse_args(argv)

    cert = sv.savings_certificate(tenant_id=args.tenant, day=args.day)
    s = cert["savings"]

    if args.json:
        print(json.dumps(cert, indent=2, sort_keys=True, default=str))
        return 0

    decomp = s["decomposition"]
    base = s["billed_microusd_over_priced_base"]
    print(f"=== VSR Savings Certificate: tenant {args.tenant} day {args.day} ===")
    print(f"  rate version:             {cert.get('rate_version', '-')}")
    print(f"  priced requests (base):   {s['priced_request_count']}")
    print(f"  billed over priced base:  {_fmt_usd(base)}")
    print(f"  total billed (all reqs):  {_fmt_usd(s['total_billed_microusd_all_classes'])}")
    # NET is the headline; the decomposition is shown BELOW it, never instead of
    # it (Fable finding 4: gross must not be cherry-pickable as "the saving").
    print(f"  NET saving:               {_fmt_usd(s['net_saving_microusd'])}")
    print(f"    (+ cheaper-if-followed: {_fmt_usd(decomp['positive_deltas_microusd'])})")
    print(f"    (- dearer-if-followed:  {_fmt_usd(decomp['negative_deltas_microusd'])})")
    if base > 0:
        pct = 100.0 * s["net_saving_microusd"] / base
        print(f"  net saving vs priced base: {pct:.1f}%")
    counts = ", ".join(f"{k}={v}" for k, v in sorted(s["class_counts"].items()))
    print(f"  request classes:          {counts or '(none)'}")
    print(f"  quality measured:         {s['quality']['measured']} "
          f"({s['quality']['note']})")

    if args.detail:
        print()
        print("  span_id\tsuggested->billed\trecompute(billed)\trecompute(sug)\tsaving")
        for r in s["detail"]:
            print(
                f"  {r['span_id']}\t{r['suggested_model'] or '-'}->"
                f"{r['billed_model_id'] or '-'}\t"
                f"{_fmt_usd(r.get('recompute_billed_microusd') or 0)}\t"
                f"{_fmt_usd(r.get('recompute_suggested_microusd') or 0)}\t"
                f"{_fmt_usd(r['saving_microusd'])}"
            )
    print()
    print(f"  {NOTICE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
