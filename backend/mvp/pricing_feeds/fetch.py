"""Ops entry point: fetch prices now, show what would change, optionally store it.

Why a CLI as well as the in-process refresh: the first fetch after a cold start with
an empty snapshot is the one time a live feed sits between a request and its price.
Running this once at deploy time fills the snapshot, so every task that starts
afterwards reads real prices from the store instead of racing the feeds.

    python -m mvp.pricing_feeds.fetch                 # dry run: fetch and print a diff
    python -m mvp.pricing_feeds.fetch --apply         # fetch and store the snapshot
    python -m mvp.pricing_feeds.fetch --print-prefixes  # regenerate the region table

A dry run touches the price APIs and the snapshot READ, never the write, so it is
safe to run against production credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from typing import Optional

from ..rates import RATE_FIELDS, Rate
from .composite import LivePriceSource
from .snapshot import SnapshotStore


def _fmt(rate: Optional[Rate]) -> str:
    if rate is None:
        return "-"
    return "/".join(str(getattr(rate, field)) for field in RATE_FIELDS)


def _diff(old: dict[str, Rate], new: dict[str, Rate]) -> list[str]:
    lines = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        mark = "+" if before is None else ("-" if after is None else "~")
        lines.append(f"  {mark} {key:<16} {_fmt(before)} -> {_fmt(after)}")
    return lines


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="mvp.pricing_feeds.fetch")
    parser.add_argument("--apply", action="store_true",
                        help="store the fetched table as the last-known-good snapshot")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero when the pass found something that needs a "
                             "human: a pricing key spanning two prices, a leg that "
                             "stopped being published, or a spent time budget")
    parser.add_argument("--print-prefixes", action="store_true",
                        help="print the billing-prefix table derived from the live "
                             "Price List, for regenerating the bundled document")
    args = parser.parse_args(argv)

    if args.print_prefixes:
        return _print_prefixes()



    store = SnapshotStore()
    stored = store.load()
    # A dry run must not write, and the source persists as part of a successful
    # fetch, so the store is withheld from it unless --apply was given. The snapshot
    # that was already read is passed in so the diff is against what is live.
    source = LivePriceSource(store=store if args.apply else _ReadOnlyStore(stored),
                             interval_seconds=0)
    if args.json:
        # The gateway's structured logs go to stdout, and a machine-readable mode that
        # needs a human to strip log lines out of it is not machine-readable. The logs
        # still happen — on stderr. Redirecting the stream itself rather than reaching
        # into logging handlers catches the ones installed lazily on first use.
        with contextlib.redirect_stdout(sys.stderr):
            report = source.refresh()
    else:
        report = source.refresh()

    if args.json:
        print(json.dumps({
            "digest": report.digest,
            "keys": {k: {f: getattr(v, f) for f in RATE_FIELDS}
                     for k, v in sorted(report.rates.items())},
            "provenance": report.provenance,
            "unpriced": report.unpriced,
            "feed_errors": report.feed_errors,
            "unparsed": report.unparsed,
            "unparsed_samples": report.unparsed_samples,
            "unauthorized": report.unauthorized,
            "leg_regressions": report.leg_regressions,
            "widened": report.widened,
            "key_price_disagreement": report.key_price_disagreement,
            "truncated": report.truncated,
            "coverage_regressions": report.coverage_regressions,
            "errors_dropped": {k: v for k, v in report.errors_dropped.items() if v},
            "duration_seconds": round(report.duration_seconds, 3),
            "applied": bool(args.apply),
        }, indent=1, sort_keys=True))
        return _exit_code(report, strict=args.strict)

    print(f"fetched {len(report.rates)} pricing key(s) in "
          f"{report.duration_seconds:.1f}s, digest {report.digest}")
    for key in sorted(report.rates):
        print(f"  {key:<16} {_fmt(report.rates[key])}  [{report.provenance.get(key, '?')}]")
    if report.unpriced:
        print(f"\nunpriced models ({len(report.unpriced)}) — these keep the bundled floor:")
        for model, why in sorted(report.unpriced.items()):
            print(f"  {model}: {why}")
    if report.key_price_disagreement:
        print("\npricing keys that span two prices — SPLIT THESE, they are charged at "
              "the dearest member:")
        for key, models in sorted(report.key_price_disagreement.items()):
            print(f"  {key}:")
            for model, (input_micro, output_micro) in sorted(models.items()):
                print(f"    {model:<48} in={input_micro} out={output_micro}")
    if report.coverage_regressions:
        print("\nkeys the stored version has and this pass did not produce (still served "
              "from the stored value):")
        print("  " + ", ".join(report.coverage_regressions))
    if report.leg_regressions:
        print("\nlegs that stopped being published (now charged from the stored value):")
        for key, legs in sorted(report.leg_regressions.items()):
            print(f"  {key}: {', '.join(legs)}")
    if report.widened:
        print("\nlegs priced from outside the region or scope a request would use "
              "(charged at the dearest published number):")
        for key, legs in sorted(report.widened.items()):
            print(f"  {key}: {', '.join(legs)}")
    if report.unauthorized:
        print("\nmodels this account may not read the offer for (grant "
              "bedrock:ListFoundationModelAgreementOffers, or accept the floor):")
        for model, feed in sorted(report.unauthorized.items()):
            print(f"  {model} [{feed}]")
    if report.truncated:
        print("\n[WARNING] the pass did not ask about everything (a spent budget or a "
              "page cap); the table is partial and the rest keeps the layer below")
    if report.unparsed:
        print("\nunparsed rate names (a grammar this build does not know):")
        for feed, count in sorted(report.unparsed.items()):
            samples = ", ".join(report.unparsed_samples.get(feed, [])[:3])
            print(f"  {feed}: {count} ({samples})")
    if report.feed_errors:
        print("\nfeed errors:")
        for feed, errors in sorted(report.feed_errors.items()):
            for error in errors[:5]:
                print(f"  {feed}: {error}")
    changes = _diff(stored.rates if stored else {}, report.rates)
    print("\nchange vs stored snapshot:" if changes else "\nno change vs stored snapshot")
    for line in changes:
        print(line)
    if not args.apply:
        print("\n(dry run — nothing stored; re-run with --apply)")
    return _exit_code(report, strict=args.strict)


def _exit_code(report, *, strict: bool) -> int:
    """0 when the pass produced a table, 1 when it produced nothing.

    `--strict` adds 2 for the findings that need a person: a key spanning two prices
    has to be split in the registry, a leg that stopped being published has to be
    looked at, and a spent budget means the table is partial. They are not failures of
    the fetch — the numbers it did produce are good — so they get their own code rather
    than being mixed in with "no prices at all".
    """
    if not report.rates:
        return 1
    if strict and (report.key_price_disagreement or report.leg_regressions
                   or report.coverage_regressions or report.truncated):
        return 2
    return 0


class _ReadOnlyStore:
    """A store that reads the real snapshot and drops every write.

    So the dry run diffs against live data without the risk of a half-tested table
    becoming the last-known-good one.
    """

    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def load(self):
        return self._snapshot

    def save(self, rates, provenance, live_classes=None):  # noqa: D401 — inert.
        return None


def _print_prefixes() -> int:
    """Derive prefix -> region from the live Price List and print it as the document.

    Every Bedrock product carries both `regionCode` and a `usagetype` whose first
    segment is the region's billing prefix, so the table is a projection of AWS's own
    data. Printed rather than written: the bundled document is reviewed like code.
    """
    import boto3

    from .price_list import ENDPOINT_REGION_ENV, _DEFAULT_ENDPOINT_REGION

    client = boto3.client(
        "pricing",
        region_name=os.getenv(ENDPOINT_REGION_ENV) or _DEFAULT_ENDPOINT_REGION,
    )
    seen: dict[str, dict[str, int]] = {}
    for service in ("AmazonBedrock", "AmazonBedrockFoundationModels",
                    "AmazonBedrockService"):
        token = None
        while True:
            kwargs = {"ServiceCode": service, "MaxResults": 100}
            if token:
                kwargs["NextToken"] = token
            response = client.get_products(**kwargs)
            for raw in response.get("PriceList", ()):
                product = json.loads(raw)["product"]["attributes"]
                region = product.get("regionCode")
                usagetype = product.get("usagetype") or ""
                prefix = usagetype.split("-", 1)[0]
                if not region or not prefix.isupper() or not prefix.isalnum():
                    continue
                seen.setdefault(prefix, {}).setdefault(region, 0)
                seen[prefix][region] += 1
            token = response.get("NextToken")
            if not token:
                break
    prefixes = {}
    for prefix, regions in sorted(seen.items()):
        best = max(regions.items(), key=lambda kv: kv[1])
        if len(regions) > 1:
            print(f"# ambiguous prefix {prefix}: {regions}", file=sys.stderr)
        prefixes[prefix] = best[0]
    print(json.dumps({"schema_version": 1, "prefixes": prefixes}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover — ops entry point.
    raise SystemExit(main())
