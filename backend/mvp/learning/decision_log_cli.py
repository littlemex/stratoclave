"""Internal ops CLI for the routing decision log (P0).

    python -m mvp.learning.decision_log_cli --tenant <id> --day YYYYMMDD [--json]

Dumps the recorded routing decisions/outcomes for a (tenant, day) and the summed
savings. This is an INTERNAL tool (reads DynamoDB directly, no admin API / UI);
it deliberately prints a fixed honesty notice so the number is never mistaken for
a billed saving. There is no admin read API yet (that is P1) — so this unit does
NOT open a new client contract surface / CLI-UI coverage gate.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import decision_log as dl

NOTICE = (
    "NOTE: savings are ESTIMATED counterfactuals (not measured) and NOT billed "
    "savings. The figure is a PARTIAL SUM over the recorded/covered spans only — "
    "NOT a lower bound: some spans have negative savings (router escalated) and "
    "some spans are uncovered, so the population total may be higher or lower. "
    f"Basis: {dl.SAVINGS_BASIS}."
)


def _fmt_usd(microusd: int) -> str:
    neg = microusd < 0
    a = abs(int(microusd))
    return f"{'-' if neg else ''}${a // 1_000_000}.{a % 1_000_000:06d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="decision_log_cli")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--day", required=True, help="YYYYMMDD (UTC)")
    ap.add_argument("--json", action="store_true", help="emit raw JSON")
    args = ap.parse_args(argv)

    summary = dl.day_summary(tenant_id=args.tenant, day=args.day)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0

    print(f"=== Routing decisions: tenant {args.tenant} day {args.day} ===")
    print(f"  decisions:                {summary['decision_count']}")
    print(f"  outcomes:                 {summary['outcome_count']}")
    print(f"  decisions w/o outcome:    {summary['decisions_without_outcome']} (coverage gap)")
    print(
        f"  savings vs requested:     {_fmt_usd(summary['savings_vs_requested_microusd_partial_sum'])}"
        f"  (partial sum over {summary['savings_vs_requested_sample']} spans,"
        f" {summary['savings_vs_requested_negative_sample']} negative)"
    )
    print(
        f"  savings vs max-servable:  {_fmt_usd(summary['savings_vs_max_servable_microusd_partial_sum'])}"
        f"  (partial sum over {summary['savings_vs_max_servable_sample']} spans,"
        f" {summary['savings_vs_max_servable_negative_sample']} negative)"
    )
    print()
    print(f"  {NOTICE}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
