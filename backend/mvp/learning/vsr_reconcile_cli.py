"""Internal ops CLI for offline VSR billing reconciliation (P0).

    python -m mvp.learning.vsr_reconcile_cli --tenant <id> --day YYYYMMDD [--json]

Joins the VSR decision log against billed UsageLogs for a (tenant, day) and
prints, for every VSR-acted request: what the VSR advised, how the trust
boundary treated it, and what it was billed. This is an INTERNAL tool (reads
DynamoDB directly, no admin API / UI) — the same posture as decision_log_cli.

It reports ONLY what Stratoclave owns at the boundary (billing reconciliation,
enforcement integrity, coverage). The VSR's own routing-quality metrics live in
the VSR's Prometheus/Grafana — this tool never re-derives them.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import vsr_reconcile as vr

NOTICE = (
    "NOTE: billed cost is summed over MATCHED rows only (a PARTIAL SUM — "
    "unsettled decisions are excluded, not counted as 0). 'violation' = a HARD "
    "pin whose billed model differs from the advised one (a trust-boundary "
    "breach to investigate); 'unsettled' = a decision with no billed usage row "
    "(request failed before settle or a dropped write); 'indeterminate' = a HARD "
    "pin whose advised or billed model could not be resolved (a data gap — NOT "
    "counted as a breach, but a SUSTAINED rise likely means a registry/retirement "
    "gap hiding real violations, so alarm on it too). Routing-quality metrics "
    "are the VSR's own; this tool covers only the Stratoclave boundary."
)


def _fmt_usd(microusd: int) -> str:
    neg = microusd < 0
    a = abs(int(microusd))
    return f"{'-' if neg else ''}${a // 1_000_000}.{a % 1_000_000:06d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="vsr_reconcile_cli")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--day", required=True, help="YYYYMMDD (UTC)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    ap.add_argument("--rows", action="store_true",
                    help="also list every VSR-acted request row")
    ap.add_argument("--fail-on-violation", action="store_true",
                    help="exit non-zero (2) when any enforcement violation is "
                         "found, so this can gate a CI/ops alarm")
    args = ap.parse_args(argv)

    report = vr.reconcile_day(tenant_id=args.tenant, day=args.day)
    s = report["summary"]
    # A violation (a HARD pin whose BILLED model differs from the advised one) is
    # the one finding an operator must never miss; optionally make it a non-zero
    # exit so a cron/CI wrapper can alarm on it.
    rc = 2 if (args.fail_on_violation and s["enforcement_violation"] > 0) else 0

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return rc

    print(f"=== VSR billing reconciliation: tenant {args.tenant} day {args.day} ===")
    print(f"  VSR-acted requests:       {s['vsr_acted_count']}")
    print(f"  matched to a usage row:   {s['matched_count']}")
    print(f"  unsettled (no usage):     {s['unsettled_count']} (coverage gap)")
    print(f"  billed (matched sum):     {_fmt_usd(s['billed_microusd_matched_sum'])}")
    print(f"  enforcement — honored:    {s['enforcement_honored']}")
    print(f"  enforcement — VIOLATION:  {s['enforcement_violation']}")
    print(f"  enforcement — n/a:        {s['enforcement_na']}")
    print(f"  enforcement — unsettled:  {s['enforcement_unsettled']}")
    print(f"  enforcement — indeterm.:  {s['enforcement_indeterminate']} (missing model data)")
    if s.get("enforcement_unknown"):
        print(f"  enforcement — UNKNOWN:    {s['enforcement_unknown']} (unexpected verdict)")
    if s["by_decision"]:
        hist = ", ".join(f"{k}={v}" for k, v in sorted(s["by_decision"].items()))
        print(f"  by decision:              {hist}")

    if args.rows:
        print()
        # span ids are uuids (36 chars) — never truncate; a violation must be
        # uniquely locatable. One row per line, fields tab-separated.
        print("  span_id\tdecision\tadvised->committed\tbilled_model\tcost\tenforce")
        for r in report["rows"]:
            cost = _fmt_usd(r["cost_microusd"]) if r["cost_microusd"] is not None else "-"
            print(
                f"  {r['span_id']}\t{r['vsr_decision']}\t"
                f"{r['suggested_model'] or '-'}->{r['chosen_model'] or '-'}\t"
                f"{r['billed_model_id'] or '-'}\t{cost}\t{r['enforcement']}"
            )
    print()
    print(f"  {NOTICE}")
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
