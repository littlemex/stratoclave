"""The live price source: several feeds, one table, one ladder.

This is the object `mvp.price_sources` registers, so from the charging path's point
of view nothing changed — it still asks a source for a whole `{pricing_key: Rate}`
table on the pricing cache's refresh interval, and a source that raises still leaves
the last good table in force. What is new is underneath:

    admin override  >  a fresh fetch  >  the last fetch that succeeded  >  bundled floor
    (mvp.pricing)      (feeds here)      (snapshot.SnapshotStore)         (defaults/pricing.json)

Three feeds cover the three ways a model gets a price, and which one answers is
decided by the APIs at runtime rather than by a hardcoded list: the Marketplace-
metered families answer on `ListFoundationModelAgreementOffers`, the AWS-billed
families answer on the Price List, and self-hosted capacity answers from the
operator's own document. A model none of them price keeps the bundled floor.

Four rules make this safe to put in front of money, and each one exists because the
alternative under-charges or lies:

1. **A key is published only when all four legs have a number with a source.** Live
   where a feed published one, then the stored version, then the bundled floor — and if a
   leg has none of those, the key is left to the layer below rather than charged at zero.
2. **Where a choice exists, the dearer number wins** — across the regions a request
   could fail over to, and across the models that share a pricing key. A pricing key
   is a tier, and tiers are not uniform: `opus` spans Claude Opus 4.1 at $15/MTok
   input and Opus 5 at $5.50, so a tier priced at its cheapest member under-charges
   every request that used the expensive one.
3. **Rounding to integer micro-USD rounds up.** Truncation is a discount nobody
   granted.
4. **Absence never lowers a price.** A feed that goes quiet, a grammar that changes,
   a key that disappears from a response: all of them fall through to the previous
   layer, and the fall is reported (`price_feed_coverage_regression`) rather than
   inferred from the numbers.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal
from typing import Iterable, Optional, Sequence

from core.logging import get_logger

from ..rates import RATE_FIELDS, Rate
from .base import Feed, FeedRequest, FeedResult
from .dimensions import (
    TOKEN_CLASSES,
    base_model_id,
    scope_for_model_id,
    select,
    unknown_profile_prefix,
)
from .snapshot import Snapshot, SnapshotStore, digest_of

logger = get_logger(__name__)

NAME = "bedrock-live"

INTERVAL_ENV = "STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS"
# The pricing cache asks a source for a table every 60 s. Calling two AWS APIs for
# every registered model on that cadence would be a self-inflicted throttle for data
# that moves monthly, so the source answers from memory between fetches. An hour is
# short enough that a price change is picked up the same day and long enough that a
# fleet of tasks is not a load generator.
DEFAULT_INTERVAL_SECONDS = 3600
# Two budgets, because the two callers want opposite things and one number cannot serve
# both. A fetch triggered by a request (the first one after a cold start with no
# snapshot) must not hold the caller up: it stops early with a partial table, which is
# safe because every key it did not reach keeps the layer below. An explicit refresh —
# the ops CLI at deploy time, a scheduled job — has nobody waiting and wants the whole
# table, so it gets a budget long enough to finish. Measured: a twenty-model registry
# takes ~6 s against real Bedrock with the agreement feed's pool, and ~40 s without it.
BUDGET_ENV = "STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS"
DEFAULT_BUDGET_SECONDS = 15.0
REFRESH_BUDGET_ENV = "STRATOCLAVE_PRICE_FEED_REFRESH_BUDGET_SECONDS"
DEFAULT_REFRESH_BUDGET_SECONDS = 300.0

_MICRO = Decimal(1)


@dataclass
class FetchReport:
    """What one pass produced. Returned to the ops CLI and logged; never charged."""

    rates: dict[str, Rate] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)
    # Registry model ids no feed could price, and why, so an operator can see that a
    # model is on the bundled floor instead of discovering it in an invoice.
    unpriced: dict[str, str] = field(default_factory=dict)
    # Model ids a provider refused to describe for this account. Separate from
    # `unpriced` reasons so an operator can act on it (grant the permission) rather
    # than hunt for a bug, and so a live grammar check can exclude them.
    unauthorized: dict[str, str] = field(default_factory=dict)
    feed_errors: dict[str, list[str]] = field(default_factory=dict)
    unparsed: dict[str, int] = field(default_factory=dict)
    unparsed_samples: dict[str, list[str]] = field(default_factory=dict)
    # feed name -> how many of its errors did not fit its bounded list.
    errors_dropped: dict[str, int] = field(default_factory=dict)
    # pricing key -> token classes a feed published this time. Carried so the next
    # fetch can tell "this leg is still live" from "this leg stopped being readable
    # and is now being re-published from the snapshot forever".
    live_classes: dict[str, frozenset[str]] = field(default_factory=dict)
    # pricing key -> legs that WERE live and are not any more. The leg-level twin of
    # `coverage_regressions`: a key can stay present while one of its legs quietly
    # freezes, which is the failure the key-level check cannot see.
    leg_regressions: dict[str, list[str]] = field(default_factory=dict)
    # Keys the stored version has and this pass did not produce. Structured as well as
    # logged, because it is the signal that most wants acting on and a log line is not
    # something a deploy gate can read.
    coverage_regressions: list[str] = field(default_factory=list)
    # pricing key -> legs priced by looking outside the region or scope asked for.
    widened: dict[str, list[str]] = field(default_factory=dict)
    # pricing key -> {model id: (input, output) in micro-USD} where the models sharing
    # a key are NOT at one price. Named for what it is — the members disagree — rather
    # than "spread", which reads as a bid/ask. Structured rather than log-only, because
    # the action it calls for (split the key) belongs to whoever reads the report.
    key_price_disagreement: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    duration_seconds: float = 0.0
    # True when ANY feed stopped before it had asked about everything — a whole feed
    # skipped, models left unasked, a region's pages cut short. Named for the fact rather
    # than for one of its causes, because `--strict` and the operator both need "this
    # table is partial", not "one particular way of being partial happened".
    truncated: bool = False

    @property
    def digest(self) -> str:
        return digest_of(self.rates)


class LivePriceSource:
    """A `mvp.price_sources.PriceSource` backed by the feeds in this package."""

    name = NAME

    def __init__(
        self,
        feeds: Optional[Sequence[Feed]] = None,
        *,
        store: Optional[SnapshotStore] = None,
        registry: Optional[Sequence] = None,
        interval_seconds: Optional[float] = None,
        clock=time.time,
    ) -> None:
        self._feeds: Optional[Sequence[Feed]] = feeds
        self._store = store if store is not None else SnapshotStore()
        self._registry = registry
        self._interval = interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._table: dict[str, Rate] = {}
        self._provenance: dict[str, str] = {}
        self._snapshot: Optional[Snapshot] = None
        self._snapshot_loaded = False
        self._last_fetch_at = 0.0
        self._fetching = False
        self._live_classes: dict[str, frozenset[str]] = {}
        self._last_report: Optional[FetchReport] = None

    # ----- wiring -------------------------------------------------------------
    def _default_feeds(self) -> Sequence[Feed]:
        from .agreement import AgreementFeed
        from .price_list import PriceListFeed
        from .selfhosted import SelfHostedFeed

        return (AgreementFeed(), PriceListFeed(), SelfHostedFeed())

    def _entries(self) -> Sequence:
        if self._registry is not None:
            return self._registry
        from ..models import registry_entries

        return registry_entries()

    def _interval_seconds(self) -> float:
        if self._interval is not None:
            return float(self._interval)
        raw = os.getenv(INTERVAL_ENV)
        if raw:
            try:
                value = float(raw)
                if value >= 0:
                    return value
            except ValueError:
                pass
        return float(DEFAULT_INTERVAL_SECONDS)

    # ----- the PriceSource contract ------------------------------------------
    def load(self) -> dict[str, Rate]:
        """The whole effective table this source can vouch for.

        Never raises for a data reason: an empty return is legal and means "no
        opinion", which `mvp.pricing` merges as "keep the floor". The one thing that
        does propagate is a programming error, which should fail loudly in tests
        rather than be absorbed here.
        """
        with self._lock:
            self._ensure_snapshot()
            claim = self._due() and not self._fetching
            if claim:
                # Claimed inside the lock, run outside it. `mvp.pricing` deliberately
                # fetches its source off its own lock so a slow feed cannot stall every
                # concurrent request; holding this one across two AWS APIs would put
                # that stall back one layer down, where it is harder to see.
                self._fetching = True
                self._last_fetch_at = self._clock()
        if claim:
            try:
                report = self._run_fetch()
            finally:
                with self._lock:
                    self._fetching = False
            self._apply(report)
        with self._lock:
            return self._merged_locked()

    def _merged_locked(self) -> dict[str, Rate]:
        merged = dict(self._snapshot.rates) if self._snapshot else {}
        merged.update(self._table)
        return merged

    def _due(self) -> bool:
        if self._last_fetch_at == 0.0:
            # Never fetched. Stated rather than left to the arithmetic: with a clock
            # that does not start at the epoch (a test, or a monotonic source) the
            # elapsed-time comparison would decide a cold process was up to date.
            return True
        return (self._clock() - self._last_fetch_at) >= self._interval_seconds()

    def _ensure_snapshot(self) -> None:
        if self._snapshot_loaded:
            return
        self._snapshot_loaded = True
        snapshot = self._store.load() if self._store else None
        self._snapshot = snapshot
        if snapshot is None:
            logger.info("price_feed_snapshot_absent")
            return
        logger.info(
            "price_feed_snapshot_loaded",
            keys=len(snapshot.rates),
            digest=snapshot.digest,
            age_seconds=int(snapshot.age_seconds) if snapshot.fetched_at else None,
            stale=snapshot.is_stale(now=self._clock()),
        )

    # ----- fetching -----------------------------------------------------------
    def refresh(self, budget_seconds: Optional[float] = None) -> FetchReport:
        """Force a fetch, with the whole-table budget. The ops CLI's entry point."""
        with self._lock:
            self._ensure_snapshot()
            self._fetching = True
            self._last_fetch_at = self._clock()
        try:
            report = self._run_fetch(budget_seconds or self._refresh_budget_seconds())
        finally:
            with self._lock:
                self._fetching = False
        self._apply(report)
        return report

    def _run_fetch(self, budget_seconds: Optional[float] = None) -> FetchReport:
        """Talk to the feeds and fold their answers. Holds no lock, changes no state."""
        started = self._clock()
        deadline = started + (budget_seconds if budget_seconds is not None
                              else self._budget_seconds())
        report = FetchReport()
        try:
            entries = [e for e in self._entries() if not getattr(e, "virtual", False)]
            feeds = self._feeds if self._feeds is not None else self._default_feeds()
            wanted = frozenset(_price_model_id(e) for e in entries)
            for entry in entries:
                prefix = unknown_profile_prefix(entry.bedrock_model_id)
                if prefix and not getattr(entry, "price_model_id", None):
                    # A cross-region prefix this build does not know. Not stripped on a
                    # guess (that mangles a bare id like `xai.grok-4.6`), so the price APIs
                    # will reject it — said out loud, with the fix, rather than surfacing
                    # as a model that quietly sits on the floor.
                    logger.warning(
                        "price_feed_unknown_profile_prefix",
                        model_id=entry.bedrock_model_id, prefix=prefix,
                        note=("set price_model_id on this registry entry to the id the "
                              "price APIs know it by"),
                    )
            results: list[tuple[str, FeedResult]] = []
            for feed in feeds:
                name = getattr(feed, "name", feed.__class__.__name__)
                if self._clock() >= deadline:
                    # Out of budget with feeds still unasked. Recorded, not hidden: the
                    # keys those feeds would have answered for keep the layer below,
                    # and a partial pass must not read as a complete one.
                    report.truncated = True
                    report.feed_errors.setdefault(name, []).append(
                        "skipped: the fetch budget was spent before this feed ran")
                    continue
                try:
                    results.append((name, feed.fetch(FeedRequest(
                        model_ids=wanted,
                        regions=frozenset(
                            r for e in entries for r in (_candidate_regions(e) or ())),
                        deadline=deadline,
                    ))))
                except Exception as exc:  # noqa: BLE001 — a feed contract violation.
                    result = FeedResult()
                    result.note_error(f"feed raised: {exc}")
                    results.append((name, result))
            self._build(entries, results, report)
        except Exception as exc:  # noqa: BLE001 — never take charging down.
            logger.warning("price_feed_fetch_failed", error=str(exc))
            report.feed_errors.setdefault("composite", []).append(str(exc)[:300])
        report.duration_seconds = max(0.0, self._clock() - started)
        return report

    def _apply(self, report: FetchReport) -> None:
        """Take what a pass produced into the served table, and persist it."""
        with self._lock:
            self._last_report = report
            self._observe(report)
            if report.rates:
                # UNION, not replacement. A pass where one feed died returns only the
                # other's keys, and replacing would throw away rates this very process
                # read an hour ago — which is the layer the snapshot cannot cover when
                # the store is unwritable (a missing IAM permission, say).
                self._table.update(report.rates)
                self._provenance.update(report.provenance)
                self._live_classes.update(report.live_classes)
                self._maybe_persist(report)

    def _budget_seconds(self) -> float:
        return _positive_env(BUDGET_ENV, DEFAULT_BUDGET_SECONDS)

    def _refresh_budget_seconds(self) -> float:
        return _positive_env(REFRESH_BUDGET_ENV, DEFAULT_REFRESH_BUDGET_SECONDS)

    def _build(self, entries: Sequence, results: Sequence[tuple[str, FeedResult]],
              report: FetchReport) -> None:
        # Report ids in ONE namespace: the registry spelling a reader recognises, not the
        # price-API spelling this module resolved internally. The same model appearing as
        # two strings in two lists is a report nobody can act on.
        registry_id_for = {_price_model_id(e): e.bedrock_model_id for e in entries}
        for name, result in results:
            if result.truncated:
                report.truncated = True
            for model_id in sorted(result.not_authorized):
                report.unauthorized[registry_id_for.get(model_id, model_id)] = name
            if result.errors:
                report.feed_errors[name] = list(result.errors)
            if result.errors_dropped:
                report.errors_dropped[name] = result.errors_dropped
            if result.unparsed:
                report.unparsed[name] = result.unparsed
                report.unparsed_samples[name] = list(result.unparsed_samples)
        # pricing key -> per-field maximum over the models that share the key, plus
        # which token classes any feed actually published and which feeds answered.
        folded: dict[str, dict[str, int]] = {}
        live_classes: dict[str, set[str]] = {}
        sources: dict[str, set[str]] = {}
        # Per key, the distinct per-model price vectors seen. A pricing key is a
        # *tier*, and a tier is only meaningful while its members cost the same; when
        # they do not, the max is charged and this records that it happened.
        disagreement: dict[str, dict[str, tuple[int, ...]]] = {}
        for entry in entries:
            model_id = _price_model_id(entry)
            answered = False
            for name, result in results:
                card = result.cards.get(model_id)
                if not card:
                    continue
                answered = True
                regions = _candidate_regions(entry)
                if regions is None:
                    # The set of regions this request could be billed in is unknown, and
                    # the selector prices at the maximum over that set. Pricing from a
                    # SMALLER set than the truth is the one way this source can publish a
                    # rate that is too LOW, which is what every other rule here prevents.
                    report.unpriced.setdefault(
                        entry.bedrock_model_id,
                        f"{name}: the failover region set is unreadable, so a fresh "
                        f"price could be lower than the region this request reaches",
                    )
                    continue
                selection = select(
                    card,
                    regions=regions,
                    scope=scope_for_model_id(entry.bedrock_model_id),
                )
                if selection is None:
                    report.unpriced.setdefault(
                        entry.bedrock_model_id,
                        f"{name}: card has no standard-tier input and output rate for "
                        f"{sorted(regions)}",
                    )
                    continue
                report.unpriced.pop(entry.bedrock_model_id, None)
                current = folded.setdefault(entry.pricing_key, {})
                for field_name, token_class in _FIELD_BY_CLASS.items():
                    if token_class not in selection.rates:
                        continue
                    value = _to_micro(selection.rates[token_class])
                    if value > current.get(field_name, -1):
                        current[field_name] = value
                    live_classes.setdefault(entry.pricing_key, set()).add(token_class)
                if selection.widened:
                    widened = report.widened.setdefault(entry.pricing_key, [])
                    for token_class in sorted(selection.widened):
                        if token_class not in widened:
                            widened.append(token_class)
                sources.setdefault(entry.pricing_key, set()).add(name)
                disagreement.setdefault(entry.pricing_key, {})[entry.bedrock_model_id] = tuple(
                    _to_micro(selection.rates[c]) if c in selection.rates else None
                    for c in ("input", "output")
                )
            if entry.pricing_key in folded:
                # Priced by one feed and refused by another: the model is not unpriced,
                # and leaving it on that list turns a report an operator acts on into
                # noise.
                report.unpriced.pop(entry.bedrock_model_id, None)
            if not answered:
                refused = report.unauthorized.get(entry.bedrock_model_id)
                if refused:
                    reason = (f"{refused}: this account is not authorized to read the "
                              f"model's agreement offer")
                else:
                    # Prefer the feed's own reason for THIS model over the generic
                    # sentence: "no feed priced this model" is true and useless.
                    reason = next(
                        (f"{name}: {result.model_errors[model_id]}"
                         for name, result in results if model_id in result.model_errors),
                        "no feed priced this model",
                    )
                report.unpriced.setdefault(entry.bedrock_model_id, reason)
        completed = {
            key: self._complete(key, values, live_classes.get(key, set()))
            for key, values in folded.items()
        }
        report.rates = {key: rate for key, rate in completed.items() if rate is not None}
        for key in [k for k, rate in completed.items() if rate is None]:
            live_classes.pop(key, None)
            sources.pop(key, None)
        report.live_classes = {key: frozenset(classes)
                               for key, classes in live_classes.items()}
        report.provenance = {
            key: _provenance_label(sorted(names), live_classes.get(key, set()))
            for key, names in sources.items()
        }
        report.leg_regressions = self._leg_regressions(report.live_classes)
        report.key_price_disagreement = {
            key: {model: list(values) for model, values in sorted(per_model.items())}
            for key, per_model in disagreement.items()
            if len({values for values in per_model.values()}) > 1
        }

    def _complete(self, key: str, published: dict[str, int],
                 live: set[str]) -> Optional[Rate]:
        """Fill the legs no feed published from the layer underneath, or give up.

        The order is the same ladder the whole source follows — last-known-good
        snapshot, then the bundled floor for this key, then the floor's `default`.
        `None` when even that fails: a leg with no reviewed number behind it takes the
        whole key out of this source's answer rather than being charged at zero. Zero
        would turn "the provider does not publish a cache rate" into "cached tokens are
        free" — a discount nobody granted — and it is reachable, because the floor loader
        absorbs a read failure and returns nothing on a broken deploy.
        """
        values: dict[str, int] = {}
        for field_name, token_class in _FIELD_BY_CLASS.items():
            if token_class in live and field_name in published:
                values[field_name] = published[field_name]
                continue
            fallback = self._fallback_leg(key, field_name)
            if fallback is None:
                logger.warning(
                    "price_feed_key_dropped_unfundable_leg",
                    pricing_key=key, leg=field_name,
                    note=("no published, stored or bundled value for this leg, so the "
                          "key is left to the layer below instead of charged at zero"),
                )
                return None
            values[field_name] = fallback
        return Rate(**values)

    def _leg_regressions(self, live: dict[str, frozenset[str]]) -> dict[str, list[str]]:
        """Legs that used to be published for a key and are not any more.

        The key-level check cannot see this: the key is still in the table, still
        charged, and its frozen leg is re-published from the snapshot on every pass. A
        promotional cache-write rate would go on being charged months after it ended,
        with nothing to look at. Comparing the live-class sets is the only place that
        shows up.
        """
        previous = dict(self._live_classes)
        if self._snapshot is not None:
            for key, classes in self._snapshot.live_classes.items():
                previous.setdefault(key, classes)
        out: dict[str, list[str]] = {}
        for key, was in previous.items():
            if key not in live:
                continue  # the whole key regressed; the key-level check owns that.
            lost = sorted(set(was) - set(live[key]))
            if lost:
                out[key] = lost
        return out

    def _fallback_leg(self, key: str, field_name: str) -> Optional[int]:
        snapshot = self._snapshot
        if snapshot is not None:
            stored = snapshot.rates.get(key)
            if stored is not None:
                return int(getattr(stored, field_name))
        floor = _floor_rates()
        rate = floor.get(key) or floor.get("default")
        return int(getattr(rate, field_name)) if rate is not None else None

    def _maybe_persist(self, report: FetchReport) -> None:
        """Store the union of what was known and what was just read.

        The union, not the fetch: a fetch that lost coverage — a renamed dimension, a
        model whose card stopped resolving — would otherwise overwrite the stored
        rate for the very keys the snapshot exists to preserve, and the next task to
        start would read the reduced table and fall to the floor for the rest. Newer
        values win per key; keys only the old snapshot had are carried forward.
        """
        previous = self._snapshot
        merged: dict[str, Rate] = dict(previous.rates) if previous else {}
        merged.update(report.rates)
        provenance: dict[str, str] = dict(previous.provenance) if previous else {}
        provenance.update(report.provenance)
        live_classes: dict[str, frozenset[str]] = (
            dict(previous.live_classes) if previous else {})
        live_classes.update(report.live_classes)
        digest = digest_of(merged)
        if previous is not None and previous.digest == digest and not previous.is_stale(
            now=self._clock()
        ):
            return
        if previous is not None and previous.digest != digest:
            logger.info(
                "price_table_changed",
                digest_old=previous.digest,
                digest_new=digest,
                changed_keys=sorted(_changed_keys(previous.rates, merged))[:20],
            )
        stored = (self._store.save(merged, provenance, live_classes)
                  if self._store else None)
        if stored is not None:
            self._snapshot = stored

    def _observe(self, report: FetchReport) -> None:
        previous = self._snapshot.rates if self._snapshot else {}
        regressed = sorted(set(previous) - set(report.rates)) if report.rates else []
        report.coverage_regressions = regressed
        if regressed:
            # The snapshot still answers for these keys (see `load`), so nothing is
            # mispriced — but a key that used to be readable and is not any more is
            # the signature of a renamed API, and it is the only warning that
            # precedes the whole feed going dark.
            logger.warning(
                "price_feed_coverage_regression",
                keys=regressed[:20],
                count=len(regressed),
                feed_errors={k: v[:2] for k, v in report.feed_errors.items()},
                unparsed=report.unparsed,
            )
        if report.leg_regressions:
            logger.warning(
                "price_feed_leg_regression",
                legs=report.leg_regressions,
                note=("these legs are being charged from the last stored value; the "
                      "provider stopped publishing a name this build can read"),
            )
        if report.widened:
            logger.info(
                "price_feed_scope_widened",
                legs=report.widened,
                note=("priced from outside the region or scope the request would use, "
                      "at the dearest published number"),
            )
        if report.key_price_disagreement:
            for key, models in sorted(report.key_price_disagreement.items()):
                logger.warning(
                    "price_feed_key_spans_prices",
                    pricing_key=key,
                    models=models,
                    note=("this key is charged at the dearest member; split it in the "
                          "model registry so each price point has its own key"),
                )
        if report.truncated:
            logger.warning("price_feed_table_partial",
                           budget_seconds=self._budget_seconds(),
                           duration_seconds=round(report.duration_seconds, 3))
        if report.unparsed:
            logger.info("price_feed_unparsed_names", counts=report.unparsed,
                        samples={k: v[:3] for k, v in report.unparsed_samples.items()})
        if report.unpriced:
            logger.info("price_feed_models_unpriced", models=sorted(report.unpriced)[:20],
                        count=len(report.unpriced))
        if report.unauthorized:
            logger.info("price_feed_not_authorized",
                        models=sorted(report.unauthorized)[:20],
                        count=len(report.unauthorized),
                        note=("grant bedrock:ListFoundationModelAgreementOffers for "
                              "these models, or accept the bundled floor for them"))
        logger.info("price_feed_fetch", keys=len(report.rates), digest=report.digest,
                    duration_ms=int(report.duration_seconds * 1000),
                    errors={k: len(v) for k, v in report.feed_errors.items()})

    # ----- read models for the admin view / CLI -------------------------------
    def last_report(self) -> Optional[FetchReport]:
        return self._last_report

    def provenance(self) -> dict[str, str]:
        with self._lock:
            merged = dict(self._snapshot.provenance) if self._snapshot else {}
            merged.update(self._provenance)
            return merged


_FIELD_BY_CLASS = {
    "input_per_mtok_microusd": "input",
    "output_per_mtok_microusd": "output",
    "cache_read_per_mtok_microusd": "cache_read",
    "cache_write_per_mtok_microusd": "cache_write",
}
# A leg added to the rate type and forgotten here would be published at whatever the layer
# below holds, silently, for every model. Asserted at import so it fails on the way in.
assert set(_FIELD_BY_CLASS) == set(RATE_FIELDS), (
    "the rate legs moved; update _FIELD_BY_CLASS with them"
)
assert set(_FIELD_BY_CLASS.values()) == set(TOKEN_CLASSES), (
    "the token classes moved; update _FIELD_BY_CLASS with them"
)


def _to_micro(usd_per_mtok: Decimal) -> int:
    """USD per MTok -> integer micro-USD per MTok, rounded UP.

    Ceiling rather than nearest: the remainder is a fraction of a micro-USD on a
    million tokens, and the only direction that cannot become an unfunded discount
    is up.
    """
    return int((Decimal(usd_per_mtok) * Decimal(1_000_000)).quantize(_MICRO, rounding=ROUND_CEILING))


def _candidate_regions(entry) -> Optional[frozenset[str]]:
    """Every region this entry's traffic could be billed in.

    A `responses` entry names the one region its endpoint serves, and that region is
    authoritative. A Converse entry can fail over, and a reservation must not be
    priced below the region the request may end up in, so the failover set is
    included.

    `None` rather than a quiet narrowing when the failover set cannot be read: the
    selector prices at the maximum over these regions, so a set missing members yields a
    rate that may be too LOW, and "absence never lowers a price" is the property this
    source exists to hold. Such a model keeps the layer below and is named in the report.
    """
    own = getattr(entry, "bedrock_region", None)
    regions = {own} if own else set()
    if getattr(entry, "wire_protocol", "messages") == "responses":
        return frozenset(regions)
    try:
        from ..routing.chains import failover_regions

        regions.update(failover_regions() or ())
    except Exception as exc:  # noqa: BLE001 — unknown, not empty.
        logger.warning("price_feed_failover_regions_unreadable", error=str(exc))
        return None
    return frozenset(r for r in regions if r)


def _positive_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return float(default)


def _price_model_id(entry) -> str:
    """The id the price APIs know this entry's model by.

    A registry entry may declare it (`price_model_id`) when the billed spelling differs
    from the invoked one; otherwise it is the invoked id with its inference-profile
    prefix removed. Declared wins, because the alternative for the one model that needs
    it is a prefix match that cannot tell a variant from a dearer sibling.
    """
    declared = getattr(entry, "price_model_id", None)
    return declared or base_model_id(entry.bedrock_model_id)


def _floor_rates() -> dict[str, Rate]:
    """The bundled floor. Read through the same loader `mvp.pricing` uses, so the
    fallback leg is the number that would have been charged anyway."""
    from ..price_sources import load_rate_document

    try:
        return load_rate_document()
    except Exception:  # noqa: BLE001 — the floor is validated at import elsewhere.
        return {}


def _provenance_label(feeds: list[str], live: set[str]) -> str:
    """`bedrock-agreement(input,output)` — which source answered, and for which legs.

    The legs matter as much as the source: a key whose cache rates come from the
    snapshot while its input rate is fresh is a different thing to defend in a dispute
    than one where a feed published all four, and the label is where that shows.
    """
    return f"{','.join(feeds)}({','.join(sorted(live))})"


def _changed_keys(old: dict[str, Rate], new: dict[str, Rate]) -> Iterable[str]:
    for key in set(old) | set(new):
        if old.get(key) != new.get(key):
            yield key


def register(replace: bool = False) -> LivePriceSource:
    """Register the live source under `bedrock-live` and return it.

    Registration is explicit and separate from import: a source that installed itself
    on import would make `STRATOCLAVE_PRICE_SOURCE` meaningless and give this package
    a say in every process that touches pricing, including offline tools.
    """
    from ..price_sources import register_price_source

    source = LivePriceSource()
    register_price_source(source, replace=replace)
    return source
