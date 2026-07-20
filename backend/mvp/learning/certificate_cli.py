"""Internal ops CLI: issue / fetch an auto-issued Savings Certificate.

    # issue + write-once persist for a (tenant, day); prints the outcome
    python -m mvp.learning.certificate_cli issue --tenant <id> --day YYYYMMDD \
        --generated-at-ms <ms>

    # fetch a stored certificate
    python -m mvp.learning.certificate_cli get --tenant <id> --day YYYYMMDD [--revision N]

The scheduled Lambda calls certificate_store.issue_for_tenants directly (passing
generated_at_ms from the EventBridge event time); this CLI is the manual /
debugging face. `issue` is write-once: re-running a day is a no-op, never an
overwrite. A day with no VSR-acted traffic (or low reconcile coverage) is NOT
issued — printed as a documented skip, never a $0 certificate.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import certificate_store as cs


def _cmd_issue(args) -> int:
    if args.generated_at_ms is None:
        print("--generated-at-ms is required (this tool never reads a clock; pass "
              "the issue timestamp explicitly).", file=sys.stderr)
        return 2
    out = cs.issue_and_store(tenant_id=args.tenant, day=args.day,
                             generated_at_ms=args.generated_at_ms)
    if out.issued:
        print(f"issued: tenant={out.tenant_id} day={out.day} (write-once; "
              "re-run is a no-op)")
        if args.json and out.certificate is not None:
            print(json.dumps(out.certificate, indent=2, default=str))
        return 0
    print(f"NOT issued: tenant={out.tenant_id} day={out.day} "
          f"reason={out.skip_reason} (a documented skip, NOT a $0 certificate)")
    return 0


def _cmd_get(args) -> int:
    env = cs.get_certificate(tenant_id=args.tenant, day=args.day, revision=args.revision)
    if env is None:
        print(f"no certificate for tenant={args.tenant} day={args.day} "
              f"revision={args.revision}", file=sys.stderr)
        return 1
    print(json.dumps(env, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="certificate_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("issue", help="issue + write-once persist a certificate")
    pi.add_argument("--tenant", required=True)
    pi.add_argument("--day", required=True, help="YYYYMMDD")
    pi.add_argument("--generated-at-ms", type=int, default=None,
                    help="issue timestamp in epoch ms (required; no implicit clock)")
    pi.add_argument("--json", action="store_true", help="print the full envelope")
    pi.set_defaults(fn=_cmd_issue)

    pg = sub.add_parser("get", help="fetch a stored certificate")
    pg.add_argument("--tenant", required=True)
    pg.add_argument("--day", required=True, help="YYYYMMDD")
    pg.add_argument("--revision", type=int, default=0)
    pg.set_defaults(fn=_cmd_get)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
