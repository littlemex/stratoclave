"""What a period WOULD have cost at another rate table.

Tokens are the record of origin. Every terminal money event in the ledger carries the
rating it was charged under — per leg, the token count, the rate applied, and the cost —
so a charge is not an opaque number: it is arithmetic over facts that are all still
there. `CreditLedgerRepository.rating_replay_mismatches` already replays each event
against its OWN rate to prove the arithmetic reproduces. This module does the other
replay, the one repricing needs: the same tokens against a DIFFERENT table.

That makes a price correction a report rather than an archaeology exercise. AWS publishes
no end date for a promotional price, so a rate can turn out to have been wrong for a
week before anyone notices; when that happens the question is "what should this period
have cost", and the answer is computable from what was stored.

**This module writes nothing.** It reads the ledger and returns two totals — as-charged
and as-repriced — with the difference broken down per pricing key. Moving money to close
that difference is a separate decision with its own contract: the charge of record stays
what it was, and a correction would be a new, idempotent adjustment event rather than an
edit (`docs/design/price-feeds.md`, section 7). A report that cannot alter the ledger is
also a report that is safe to run against production.

    python -m mvp.reprice --tenant acme --period 2026-08 --at-version <digest>
    python -m mvp.reprice --tenant acme --period 2026-08 --at floor
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from boto3.dynamodb.conditions import Key

from core.logging import get_logger

from .pricing import mtok_cost_for_rounding
from .rates import RATE_FIELDS, Rate

logger = get_logger(__name__)

# The ledger stores a rating whose component names are the billable legs; the rate table
# names the same legs as `*_per_mtok_microusd` fields. One map, asserted against the leg
# registry at import so the two cannot drift apart silently.
_RATE_FIELD_BY_COMPONENT = {
    "input": "input_per_mtok_microusd",
    "output": "output_per_mtok_microusd",
    "cache_read": "cache_read_per_mtok_microusd",
    "cache_write": "cache_write_per_mtok_microusd",
}
assert set(_RATE_FIELD_BY_COMPONENT.values()) == set(RATE_FIELDS), (
    "the rate legs moved; update mvp.reprice's component map with them"
)


@dataclass
class RepriceReport:
    """Two totals over the same tokens, and everything needed to trust the difference."""

    tenant_id: str
    period: str
    # Which table the recompute used, and where it came from.
    target: str
    as_charged_microusd: int = 0
    as_repriced_microusd: int = 0
    # pricing key -> (as-charged, as-repriced, events) so a difference can be attributed
    # rather than merely stated.
    by_pricing_key: dict[str, dict[str, int]] = field(default_factory=dict)
    events_priced: int = 0
    # Every terminal money event in the period, whether or not it could be repriced. The
    # denominator: a total that silently drops part of a period looks like a total.
    events_seen: int = 0
    # Events the recompute could not cover, by reason. Reported rather than skipped: a
    # total that silently omits part of a period is worse than no total, because it looks
    # like one.
    not_repriced: dict[str, int] = field(default_factory=dict)
    # Keys the target table does not price at all. Their events stay in `not_repriced`.
    keys_missing_from_target: list[str] = field(default_factory=list)

    @property
    def difference_microusd(self) -> int:
        return self.as_repriced_microusd - self.as_charged_microusd

    @property
    def complete(self) -> bool:
        """True when every event in the period was repriced.

        Reported rather than assumed. A difference computed over some of a period is not a
        smaller version of the right answer, it is a different question, and the caller has
        to be able to tell which one it got.
        """
        return self.events_seen == self.events_priced and not self.not_repriced

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "period": self.period,
            "target": self.target,
            "as_charged_microusd": self.as_charged_microusd,
            "as_repriced_microusd": self.as_repriced_microusd,
            "difference_microusd": self.difference_microusd,
            "by_pricing_key": self.by_pricing_key,
            "events_priced": self.events_priced,
            "events_seen": self.events_seen,
            "complete": self.complete,
            "not_repriced": self.not_repriced,
            "keys_missing_from_target": sorted(self.keys_missing_from_target),
        }


def reprice_period(
    *,
    tenant_id: str,
    period: str,
    target_rates: Mapping[str, Rate],
    target_label: str,
    repo=None,
) -> RepriceReport:
    """Recompute `period`'s charges at `target_rates`, from the tokens the ledger stored.

    The rounding policy is the one the original charge froze, not today's default: a
    recompute that quietly changed how fractions round would report a difference that is
    partly its own doing.
    """
    from dynamo.credit_ledger import CreditLedgerRepository, ledger_pk

    repo = repo or CreditLedgerRepository()
    report = RepriceReport(tenant_id=tenant_id, period=period, target=target_label)
    missing: set[str] = set()
    for event in _charge_events(repo, ledger_pk(tenant_id, period)):
        report.events_seen += 1
        # As-charged is the ledger's own money move, not the rating's self-report. They
        # must agree (the ledger's replay audit checks exactly that), and when they do not,
        # the charge of record is the settled delta and the rating is the thing in doubt.
        charged = _nonneg_int(event.get("settled_delta_microusd"))
        if charged is None:
            _count(report.not_repriced, "settled_delta_unreadable")
            continue
        report.as_charged_microusd += charged
        raw_rating = event.get("rating")
        if not raw_rating:
            # A terminal that moved money with no frozen rating cannot be repriced — and it
            # still belongs in the as-charged total, or the difference is measured against
            # a smaller period than the one asked about.
            _count(report.not_repriced, "missing_rating")
            continue
        try:
            rating = json.loads(raw_rating)
            components = rating["components"]
            rounding = str(rating.get("rounding", "ceil"))
            pricing_key = str(rating.get("pricing_key") or "")
            rating_total = _nonneg_int(rating.get("total_cost_microusd"))
        except (ValueError, KeyError, TypeError):
            _count(report.not_repriced, "unparseable_rating")
            continue
        if rating_total is None or rating_total != charged:
            # The rating does not describe the money that moved, so its token counts are
            # not evidence of what this charge was for.
            _count(report.not_repriced, "rating_disagrees_with_settled_delta")
            continue
        if not pricing_key:
            _count(report.not_repriced, "rating_without_pricing_key")
            continue
        if not isinstance(components, Mapping) or not components:
            _count(report.not_repriced, "rating_without_components")
            continue
        target = target_rates.get(pricing_key)
        if target is None:
            missing.add(pricing_key)
            _count(report.not_repriced, "pricing_key_absent_from_target")
            continue
        repriced = 0
        ok = True
        for name, component in components.items():
            field_name = _RATE_FIELD_BY_COMPONENT.get(name)
            if field_name is None:
                # A leg this build does not know: refuse the event rather than price it
                # at three legs out of four.
                ok = False
                break
            tokens = _nonneg_int((component or {}).get("tokens")
                                 if isinstance(component, Mapping) else None)
            if tokens is None:
                ok = False
                break
            repriced += mtok_cost_for_rounding(
                tokens, int(getattr(target, field_name)), rounding)
        if not ok:
            _count(report.not_repriced, "unknown_component_leg")
            continue
        report.events_priced += 1
        report.as_repriced_microusd += repriced
        bucket = report.by_pricing_key.setdefault(
            pricing_key, {"as_charged_microusd": 0, "as_repriced_microusd": 0, "events": 0})
        bucket["as_charged_microusd"] += charged
        bucket["as_repriced_microusd"] += repriced
        bucket["events"] += 1
    report.keys_missing_from_target = sorted(missing)
    logger.info("reprice_report", tenant_id=tenant_id, period=period,
                target=target_label, events=report.events_priced,
                events_seen=report.events_seen, complete=report.complete,
                difference_microusd=report.difference_microusd,
                not_repriced=report.not_repriced)
    return report


def _charge_event_types() -> frozenset[str]:
    """The event types whose settled delta IS the period's charge.

    Taken from the ledger's own constants rather than spelled out here. Spelling them out
    is how this module shipped reading `"TERMINAL"` — the name of a SORT KEY, not of any
    event type the writer emits — so every real charge was invisible and the report said
    `complete`. A name this module invents cannot disagree with the ledger's writer, which
    is exactly why it must not invent one.

    `SETTLE` and `LATE_SETTLE` only. `RELEASE` and `RECLAIM` return a reservation rather
    than charging for usage; `OWED_SETTLE` and `RETAINED` are evidence of a charge that has
    not been posted, so counting them would double the money once the real settle lands.
    """
    from dynamo.credit_ledger import EV_LATE_SETTLE, EV_SETTLE

    return frozenset({EV_SETTLE, EV_LATE_SETTLE})


def _charge_events(repo, pk: str) -> Iterable[Mapping[str, Any]]:
    """Every event in the partition whose settled delta is part of the charge, rated or not.

    Read strongly and paged. Strongly because a report that quietly omits a settle written
    a second ago reads as a complete period; rated-or-not because an event with no frozen
    rating still moved money, and leaving it out of the as-charged total would measure the
    difference against a period that is not the one asked about.
    """
    charge_types = _charge_event_types()
    kwargs: dict[str, Any] = {
        "KeyConditionExpression": Key("pk").eq(pk),
        "ConsistentRead": True,
        "ProjectionExpression": ("sk, hold_id, event_type, rating, "
                                 "settled_delta_microusd"),
    }
    while True:
        response = repo._table.query(**kwargs)
        for item in response.get("Items", ()):
            if item.get("settled_delta_microusd") is None:
                continue
            if str(item.get("event_type") or "") not in charge_types:
                continue
            yield item
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def _nonneg_int(value: Any) -> Optional[int]:
    """`value` as a non-negative int, or `None`.

    Rejects bool explicitly: `int(True)` is 1, and a rating whose total is `true` would
    otherwise be read as one micro-USD and quietly become part of a difference report.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _count(counter: dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


# --- target tables ----------------------------------------------------------
def target_from_floor() -> tuple[dict[str, Rate], str]:
    """The bundled document — the table a deployment charges when nothing else answers."""
    from .price_sources import load_rate_document

    return load_rate_document(), "floor"


def target_from_effective() -> tuple[dict[str, Rate], str]:
    """The DURABLE effective table: floor, then the stored version, then admin overrides.

    Named for what it can see. A task that fetched fresh prices and failed to persist them
    charges from its own memory, and no report can read another process's memory — so this
    is what a restarting task would charge, not a guarantee about what every task is
    charging this second. The label says `durable-effective` for that reason.

    Assembled here rather than through `mvp.pricing.effective_rates`, which resolves the
    active price source: on a cold process that would run a live fetch and persist a new
    stored version, so a tool that says it writes nothing would write the very state
    charging depends on. The layers are read directly instead — the bundled floor, then the
    current stored version, then the admin overrides — which is the same order and none of
    the side effects.
    """
    from dynamo.pricing_config import PricingConfigRepository

    from .pricing_feeds.snapshot import SnapshotStore

    rates, _ = target_from_floor()
    labels = ["floor"]
    stored = SnapshotStore().load()
    if stored is not None:
        rates = {**rates, **stored.rates}
        labels.append(f"feed-version({stored.digest})")
    repo = PricingConfigRepository()
    try:
        version = repo.current_version()
        if version:
            rates = {**rates, **repo.load_rates(version)}
            labels.append(f"override({version})")
    except Exception as exc:  # noqa: BLE001 — a report must not fail on a missing table.
        logger.warning("reprice_override_read_failed", error=str(exc))
    return rates, "durable-effective(" + " + ".join(labels) + ")"


def target_from_feed_version(version: str) -> tuple[dict[str, Rate], str]:
    """A stored price-feed version, by its digest.

    This is what the version history is for: the table that WAS in force on a given day
    is readable, so "what should this period have cost" is a lookup and a multiplication
    rather than a reconstruction from memory.
    """
    from .pricing_feeds.snapshot import SnapshotStore

    snapshot = SnapshotStore().load_version(version)
    if snapshot is None:
        raise ValueError(f"no stored price-feed version {version!r}")
    return snapshot.rates, f"feed-version({version})"


def _usd(micro: int) -> str:
    sign = "-" if micro < 0 else ""
    n = abs(int(micro))
    return f"{sign}${n // 1_000_000}.{n % 1_000_000:06d}"


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover — ops entry point.
    import argparse

    parser = argparse.ArgumentParser(
        prog="mvp.reprice",
        description="Recompute a period's charges at another rate table. Reads only.",
    )
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--period", required=True, help="the ledger period, e.g. 2026-08")
    parser.add_argument("--at", choices=("floor", "effective"), default="effective",
                        help="which table to recompute at (default: effective)")
    parser.add_argument("--at-version", help="a stored price-feed version digest")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.at_version:
        rates, label = target_from_feed_version(args.at_version)
    elif args.at == "floor":
        rates, label = target_from_floor()
    else:
        rates, label = target_from_effective()

    report = reprice_period(tenant_id=args.tenant, period=args.period,
                            target_rates=rates, target_label=label)
    if args.json:
        print(json.dumps(report.to_dict(), indent=1, sort_keys=True))
        return 0
    print(f"tenant {report.tenant_id} period {report.period} recomputed at {report.target}")
    print(f"  as charged : {_usd(report.as_charged_microusd)}")
    print(f"  as repriced: {_usd(report.as_repriced_microusd)}")
    print(f"  difference : {_usd(report.difference_microusd)} "
          f"over {report.events_priced} event(s)")
    for key, bucket in sorted(report.by_pricing_key.items()):
        delta = bucket["as_repriced_microusd"] - bucket["as_charged_microusd"]
        if not delta:
            continue
        print(f"    {key:<16} {_usd(bucket['as_charged_microusd'])} -> "
              f"{_usd(bucket['as_repriced_microusd'])}  ({_usd(delta)}, "
              f"{bucket['events']} event(s))")
    if report.not_repriced:
        print("\nnot repriced:")
        for reason, count in sorted(report.not_repriced.items()):
            print(f"  {reason}: {count}")
    if report.keys_missing_from_target:
        print(f"  pricing keys absent from the target table: "
              f"{', '.join(report.keys_missing_from_target)}")
    print("\n(read-only: the ledger is unchanged, and the charge of record stands)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
