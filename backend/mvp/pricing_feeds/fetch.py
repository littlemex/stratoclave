"""Ops entry point: fetch prices now, show what would change, optionally store it.

Why a CLI as well as the in-process refresh: the first fetch after a cold start with
an empty snapshot is the one time a live feed sits between a request and its price.
Running this once at deploy time fills the snapshot, so every task that starts
afterwards reads real prices from the store instead of racing the feeds.

    python -m mvp.pricing_feeds.fetch                 # dry run: fetch and print a diff
    python -m mvp.pricing_feeds.fetch --apply         # fetch and store the snapshot
    python -m mvp.pricing_feeds.fetch --print-prefixes  # regenerate the region table
    python -m mvp.pricing_feeds.fetch --versions      # list stored digests, newest first

A dry run touches the price APIs and the snapshot READ, never the write, so it is
safe to run against production credentials.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from ..rates import RATE_FIELDS, Rate
from .composite import LivePriceSource
from .dimensions import _read_prefix_document
from .snapshot import SnapshotStore, digest_of


# A model an operator has looked at and accepted as unpriced, one per entry. Kept as
# an env var rather than a CLI flag because the deploy gate (`--strict`) is meant to
# run unattended: the list an operator has already accepted does not change between
# runs of the same deployment, so it belongs beside the other deploy-time knobs this
# package reads, not on a command line someone has to remember to repeat.
UNPRICED_ALLOWLIST_ENV = "STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST"


def _unpriced_allowlist() -> frozenset[str]:
    raw = os.getenv(UNPRICED_ALLOWLIST_ENV) or ""
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _feed_names_in_provenance(report) -> frozenset[str]:
    """Feed names that priced at least one key this pass.

    `_provenance_label` writes `"name1,name2(legs)"`, so splitting on the first `(`
    and then on `,` recovers exactly the names `LivePriceSource._build` fed it — the
    same strings `report.unauthorized` is keyed by, since both come from a feed's own
    `name`. A feed absent from every provenance string contributed nothing this pass.
    """
    names: set[str] = set()
    for label in report.provenance.values():
        names.update(label.split("(", 1)[0].split(","))
    return frozenset(n for n in names if n)


def _strict_reasons(report) -> list[str]:
    """Which `--strict` findings this pass raised, in the order `_exit_code` checks
    them. Named by the six tokens the interface fixes, so `--help`, `_exit_code`'s
    docstring and this list never drift apart: `key_spans_prices`, `leg_regression`,
    `coverage_regression`, `budget_spent`, `feed_not_authorized`,
    `unpriced_not_allowlisted`.

    The last two read differently on purpose. `feed_not_authorized` fires whenever a
    feed that reported at least one `not_authorized` model priced NOTHING this pass —
    the only way that happens to a feed that did not merely run out of budget is that
    every model it was asked about came back denied — and it is checked without
    consulting the allowlist, because a whole catalogue going dark is not the same
    fact as a model an operator has individually accepted. `unpriced_not_allowlisted`
    is the per-model residual: anything still unpriced that the allowlist does not
    cover.
    """
    reasons = []
    if report.key_price_disagreement:
        reasons.append("key_spans_prices")
    if report.leg_regressions:
        reasons.append("leg_regression")
    if report.coverage_regressions:
        reasons.append("coverage_regression")
    if report.truncated:
        reasons.append("budget_spent")
    priced_by = _feed_names_in_provenance(report)
    if any(feed not in priced_by for feed in report.unauthorized.values()):
        reasons.append("feed_not_authorized")
    allowlist = _unpriced_allowlist()
    if any(model not in allowlist for model in report.unpriced):
        reasons.append("unpriced_not_allowlisted")
    return reasons


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
                        help="exit 2 when the pass raised a finding a person should "
                             "see, named by exactly these tokens: key_spans_prices "
                             "(a pricing key spans two prices), leg_regression (a leg "
                             "stopped being published), coverage_regression (a key the "
                             "store has that this pass did not produce), budget_spent "
                             "(the pass ran out of time), feed_not_authorized (a whole "
                             "feed was denied — unconditional, the allowlist below "
                             "cannot suppress it) and unpriced_not_allowlisted (an "
                             f"unpriced model not on {UNPRICED_ALLOWLIST_ENV})")
    parser.add_argument("--print-prefixes", action="store_true",
                        help="print the billing-prefix table derived from the live "
                             "Price List, for regenerating the bundled document")
    parser.add_argument("--versions", action="store_true",
                        help="list stored snapshot digests and timestamps, newest "
                             "first — the input reprice --at-version takes")
    args = parser.parse_args(argv)

    if args.print_prefixes:
        return _print_prefixes()
    if args.versions:
        return _print_versions(SnapshotStore())

    store = SnapshotStore()

    def _run():
        # The read below and the fetch it feeds can both log through structlog on
        # their own — a missing table name, a `ClientError`, a feed that raised — and
        # in --json mode every one of those has to land on stderr, never on the
        # stdout the caller is about to fill with one JSON document. The comment this
        # replaces claimed redirecting the stream "catches the ones installed lazily
        # on first use", which is true of handlers but not of a log line already
        # emitted before the redirect started: the fix is making sure nothing that can
        # log — this read, the fetch, and the --apply readback below — runs outside
        # the guard, not trusting the guard to catch it after the fact.
        stored = store.load()
        # A dry run must not write, and the source persists as part of a successful
        # fetch, so the store is withheld from it unless --apply was given. The
        # snapshot that was already read is passed in so the diff is against what is
        # live.
        source = LivePriceSource(store=store if args.apply else _ReadOnlyStore(stored),
                                 interval_seconds=0)
        report = source.refresh()
        # `store.save()` never raises and never tells this process whether the write
        # it just asked for actually landed — that is the whole defect --apply exists
        # to close, so the only way to know is to read the store back and check it
        # holds what this pass should have produced.
        applied_digest, apply_reason = (
            _confirm_applied(store, stored, report) if args.apply else (None, None)
        )
        return stored, report, applied_digest, apply_reason

    if args.json:
        with contextlib.redirect_stdout(sys.stderr):
            stored, report, applied_digest, apply_reason = _run()
    else:
        stored, report, applied_digest, apply_reason = _run()

    reasons = _strict_reasons(report)
    # A failed --apply is not one of the --strict findings — it is wrong whether or
    # not --strict was passed, which is the M8 defect: the documented way to avoid
    # every task racing the feeds must not exit 0 having done nothing.
    exit_code = 2 if (args.apply and apply_reason) else _exit_code(report, strict=args.strict)

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
            # The digest the store actually holds once it is confirmed, never a bare
            # echo of the flag: `false` covers both "not asked to apply" and "asked
            # to, and it did not take".
            "applied": applied_digest if applied_digest else False,
            "apply_error": apply_reason,
            "strict_reasons": reasons,
        }, indent=1, sort_keys=True))
        return exit_code

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
    if args.apply:
        if apply_reason:
            print(f"\n[ERROR] --apply did not take effect: {apply_reason}")
        else:
            print(f"\napplied — store now holds digest {applied_digest}")
    else:
        print("\n(dry run — nothing stored; re-run with --apply)")
    if args.strict and reasons:
        print("\n[STRICT] exit 2 for: " + ", ".join(reasons))
    return exit_code


def _confirm_applied(store, stored, report) -> tuple[Optional[str], Optional[str]]:
    """Read the store back and say whether --apply actually landed.

    `SnapshotStore.save` never raises: a missing table name, a missing region, a
    `ClientError`, a lost fence — all of them return `None` and the caller has no
    other signal. `expected` mirrors the exact union `_maybe_persist` writes (this
    pass's rates over whatever was already stored) so a readback that disagrees with
    it means the write did not land as this pass intended. When there is nothing to
    store — no prior snapshot and nothing fetched — there is nothing to confirm
    either; that is the ordinary "no rates" exit, not a write failure, so it is
    reported as success carrying no digest rather than an error.
    """
    expected = dict(stored.rates) if stored else {}
    expected.update(report.rates)
    if not expected:
        return None, None
    expected_digest = digest_of(expected)
    readback = store.load()
    if readback is None or readback.digest != expected_digest:
        held = readback.digest if readback is not None else "nothing"
        return None, (f"store write did not land: expected digest {expected_digest}, "
                      f"store holds {held}")
    return readback.digest, None


def _print_versions(store: SnapshotStore) -> int:
    """List every stored version newest first — what `reprice --at-version` takes.

    `SnapshotStore.versions()` already pages the partition and sorts by `created_at`
    descending, so this is a thin formatter, not a second implementation of the
    ordering.
    """
    rows = store.versions()
    if not rows:
        print("no stored versions")
        return 0
    for row in rows:
        stamp = datetime.fromtimestamp(row["created_at"], tz=timezone.utc).isoformat()
        print(f"{row['version']}  {stamp}  ({row['keys']} key(s))")
    return 0


def _exit_code(report, *, strict: bool) -> int:
    """0 when the pass produced a table, 1 when it produced nothing.

    `--strict` adds 2 for the findings that need a person, named by exactly these six
    tokens — repeated verbatim in `--help` and in what the command prints, so all
    three agree: `key_spans_prices` (a pricing key spans two prices, split it in the
    registry), `leg_regression` (a leg stopped being published), `coverage_regression`
    (a key the stored version has that this pass did not produce), `budget_spent` (the
    pass ran out of time and the table is partial), `feed_not_authorized` (a whole
    feed was denied — checked unconditionally; the allowlist below never suppresses
    it, because a catalogue going dark is not the same fact as a model an operator has
    individually accepted) and `unpriced_not_allowlisted` (a model still unpriced that
    `STRATOCLAVE_PRICE_FEED_UNPRICED_ALLOWLIST` does not cover). None of these are
    failures of the fetch — the numbers it did produce are good — so they get their
    own code rather than being mixed in with "no prices at all".
    """
    if not report.rates:
        return 1
    if strict and _strict_reasons(report):
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

    def save(self, rates, provenance, live_classes=None, *,  # noqa: D401 — inert.
             now=None, fenced_on=None):
        # `SnapshotStore.save`'s `fenced_on` is required and keyword-only, and this
        # store still gets called: a successful dry-run fetch persists through
        # whichever store the source was built with. Accepting the same keyword (and
        # the `now` beside it) keeps a dry run from raising a `TypeError` out of a
        # write it was always going to drop anyway.
        return None


def _print_prefixes() -> int:
    """Derive prefix -> region from the live Price List and print it as the document.

    Every Bedrock product carries both `regionCode` and a `usagetype` whose first
    segment is the region's billing prefix, so the table is a projection of AWS's own
    data. Printed rather than written: the bundled document is reviewed like code.

    `id_suffix_segments` is not in that projection — it is provider vocabulary this
    scan has no way to discover (see the bundled document's own `$comment`), so it is
    carried over from the current document rather than dropped. Following this tool's
    own instruction used to silently drop that key, and the loader accepts a document
    without it, so the gap is dropped models rather than a loud failure.
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
    # Start from the bundled document rather than a bare `{schema_version, prefixes}`
    # literal, so every key the loader may ever ask for — `id_suffix_segments` today,
    # whatever is added beside it later — survives a regenerate by construction
    # instead of by remembering to list it here.
    document = dict(_read_prefix_document())
    document["prefixes"] = prefixes
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover — ops entry point.
    raise SystemExit(main())
