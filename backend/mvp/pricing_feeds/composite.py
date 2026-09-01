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
from .snapshot import Snapshot, SnapshotStore, digest_of, stale_after_seconds

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
    # The budget this pass ran with. Carried rather than looked up when logging: the
    # request path and the ops refresh have different ceilings, and reporting the wrong
    # one names a knob that had nothing to do with the truncation.
    budget_seconds: float = 0.0
    # How old the stored table was when this pass ran, in wall seconds. Reported on every
    # pass that produced nothing, so a feed that has been dark for a week is a repeating
    # signal rather than one info line at task start.
    snapshot_age_seconds: Optional[float] = None
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
        budget_clock=time.monotonic,
    ) -> None:
        self._feeds: Optional[Sequence[Feed]] = feeds
        self._store = store if store is not None else SnapshotStore()
        self._registry = registry
        self._interval = interval_seconds
        # Two time sources, because two different questions are being asked. `clock` is
        # WALL time: it answers "how long ago was this instant", which is only meaningful
        # against the timestamps the store wrote. `budget_clock` is ELAPSED time, and the
        # budget is arithmetic on it alone — including the deadline handed to the feeds,
        # which compare against the very callable it came from. Sharing one clock is what
        # let a deadline be computed on one scale and read on another.
        self._clock = clock
        self._budget_clock = budget_clock
        self._lock = threading.Lock()
        # Held for the whole of a pass, so two passes cannot run at once. Separate from
        # `_lock`, which covers state swaps only and must never be held across I/O.
        self._pass_lock = threading.Lock()
        self._table: dict[str, Rate] = {}
        self._provenance: dict[str, str] = {}
        self._snapshot: Optional[Snapshot] = None
        self._snapshot_loaded = False
        # `None` rather than 0.0: with an elapsed-time source, zero is a legal reading a
        # few microseconds after start, so the sentinel has to be outside the value space.
        self._last_fetch_at: Optional[float] = None
        self._live_classes: dict[str, frozenset[str]] = {}
        self._last_report: Optional[FetchReport] = None

    # ----- wiring -------------------------------------------------------------
    def _default_feeds(self) -> Sequence[Feed]:
        from .agreement import AgreementFeed
        from .price_list import PriceListFeed
        from .selfhosted import SelfHostedFeed

        # The self-hosted feed answers from the registry THIS source was constructed with.
        # Reading a global one made a source built with an injected registry answer from two
        # different registries at once, and left a self-hosted entry that declares a billing
        # id silently unpriced.
        return (AgreementFeed(), PriceListFeed(), SelfHostedFeed(registry=self._entries()))

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
        # The store read happens off `_lock`. Claiming the pass inside the lock and
        # running it outside was already the rule for the feeds — `mvp.pricing` fetches
        # its source off its own lock so a slow feed cannot stall every concurrent
        # request — but the snapshot read and the persist were still inside it, which put
        # the same stall back one layer down where it is harder to see. A blocked
        # DynamoDB call must not stop a concurrent reader from being served the table
        # already in memory.
        self._ensure_snapshot()
        with self._lock:
            due = self._due()
        if due and self._pass_lock.acquire(blocking=False):
            try:
                with self._lock:
                    # The version this pass starts from. Captured before the fetch, so a
                    # pass that finishes after someone else moved the pointer fences on
                    # what it actually saw rather than on what it finds afterwards.
                    started_from = self._snapshot.digest if self._snapshot else None
                    self._last_fetch_at = self._budget_clock()
                report = self._run_fetch()
                self._apply(report, fenced_on=started_from)
            finally:
                self._pass_lock.release()
        with self._lock:
            return self._merged_locked()

    def _merged_locked(self) -> dict[str, Rate]:
        merged = dict(self._snapshot.rates) if self._snapshot else {}
        merged.update(self._table)
        return merged

    def _due(self) -> bool:
        if self._last_fetch_at is None:
            # Nothing fetched in THIS process — which is not the same as nothing being
            # known. A task that starts to a snapshot confirmed minutes ago has a table
            # inside the interval already, and refetching it makes the first request pay
            # for a pass the deploy step already ran. Compared against the interval and
            # not the staleness window: the question here is "has an interval elapsed
            # since this table was confirmed", and staleness is a separate label with its
            # own knob. Wall time, because the comparison is against a stored instant.
            snapshot = self._snapshot
            confirmed = (snapshot.last_seen_at or snapshot.fetched_at) if snapshot else 0.0
            if not confirmed:
                return True
            # Measured on THIS source's wall clock, not on `time.time()` directly. The two
            # are the same in production and are not the same under an injected clock, and a
            # dueness decision that ignores the clock it was given cannot be exercised.
            return (self._clock() - confirmed) >= self._interval_seconds()
        return (self._budget_clock() - self._last_fetch_at) >= self._interval_seconds()

    def _ensure_snapshot(self) -> None:
        """Read the stored version once per process. Runs OUTSIDE `_lock` — see `load`."""
        with self._lock:
            if self._snapshot_loaded:
                return
            self._snapshot_loaded = True
        snapshot = self._store.load() if self._store else None
        with self._lock:
            self._snapshot = snapshot
        if snapshot is None:
            logger.info("price_feed_snapshot_absent")
            return
        logger.info(
            "price_feed_snapshot_loaded",
            keys=len(snapshot.rates),
            digest=snapshot.digest,
            digest_verified=snapshot.digest_verified,
            age_seconds=int(snapshot.age_seconds) if snapshot.fetched_at else None,
            stale=snapshot.is_stale(now=self._clock()),
        )

    # ----- fetching -----------------------------------------------------------
    def refresh(self, budget_seconds: Optional[float] = None) -> FetchReport:
        """Force a fetch, with the whole-table budget. The ops CLI's entry point.

        Joins a pass already in flight rather than starting a second one. Two passes at
        once in one process is how the fence gets lost locally: both start from the same
        version, one persists, and the other then moves the pointer back to a table it
        read first. The joined report may say `truncated` — it was run on the request
        path's smaller budget — and that is reported rather than hidden, which is what
        `--strict` exits on.
        """
        self._ensure_snapshot()
        if not self._pass_lock.acquire(blocking=False):
            with self._pass_lock:
                pass
            with self._lock:
                return self._last_report if self._last_report else FetchReport()
        try:
            with self._lock:
                started_from = self._snapshot.digest if self._snapshot else None
                self._last_fetch_at = self._budget_clock()
            report = self._run_fetch(budget_seconds or self._refresh_budget_seconds())
            self._apply(report, fenced_on=started_from)
        finally:
            self._pass_lock.release()
        return report

    def _run_fetch(self, budget_seconds: Optional[float] = None) -> FetchReport:
        """Talk to the feeds and fold their answers. Holds no lock, changes no state."""
        started = self._budget_clock()
        budget = (budget_seconds if budget_seconds is not None
                  else self._budget_seconds())
        deadline = started + budget
        report = FetchReport()
        # What this pass actually ran with, not what the request path defaults to. A
        # refresh that truncated on 300 seconds used to be logged as truncating on 15,
        # which sends the reader to a knob that had nothing to do with it.
        report.budget_seconds = budget
        try:
            entries = [e for e in self._entries() if not getattr(e, "virtual", False)]
            feeds = self._feeds if self._feeds is not None else self._default_feeds()
            wanted = frozenset(_price_model_id(e) for e in entries)
            # ONE reading of the region policy per pass, shared by the feeds and by the
            # fold. Reading it twice let a routing config that recovered mid-pass turn an
            # incomplete regional catalogue into a "complete" answer — priced at the
            # maximum over a smaller set than the truth, which is the one way this source
            # can publish a rate that is too low.
            regions_by_entry = {
                id(entry): _candidate_regions(entry) for entry in entries
            }
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
                if self._budget_clock() >= deadline:
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
                            r for e in entries
                            for r in (regions_by_entry[id(e)] or ())),
                        deadline=deadline,
                        # The very callable the deadline was computed from. A feed that
                        # compared it against wall time would read an elapsed-time instant
                        # as either long past or unreachable.
                        clock=self._budget_clock,
                    ))))
                except Exception as exc:  # noqa: BLE001 — a feed contract violation.
                    result = FeedResult()
                    result.note_error(f"feed raised: {exc}")
                    results.append((name, result))
            self._build(entries, results, report, regions_by_entry)
        except Exception as exc:  # noqa: BLE001 — never take charging down.
            logger.warning("price_feed_fetch_failed", error=str(exc))
            report.feed_errors.setdefault("composite", []).append(str(exc)[:300])
        report.duration_seconds = max(0.0, self._budget_clock() - started)
        return report

    def _apply(self, report: FetchReport, *, fenced_on: Optional[str]) -> None:
        """Take what a pass produced into the served table, and persist it.

        The store call happens outside `_lock`: the lock is for swapping state, and a
        conditional put with a retrying client behind it is exactly the kind of wait that
        must not be visible to a concurrent reader.
        """
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
                # UNION per key, not replacement. `live_classes` answers "has this leg ever
                # been sourced live", and that is what tells a leg the provider stopped
                # publishing (keep the stored value, report the regression) from a leg no
                # provider ever published (fall to the current floor). Replacing the set
                # made one quiet pass erase the distinction, and the value then jumped to
                # the floor for a leg that had a perfectly good stored number.
                for key, classes in report.live_classes.items():
                    self._live_classes[key] = self._live_classes.get(
                        key, frozenset()) | classes
        if report.rates:
            self._maybe_persist(report, fenced_on=fenced_on)

    def _budget_seconds(self) -> float:
        return _positive_env(BUDGET_ENV, DEFAULT_BUDGET_SECONDS)

    def _refresh_budget_seconds(self) -> float:
        return _positive_env(REFRESH_BUDGET_ENV, DEFAULT_REFRESH_BUDGET_SECONDS)

    def _build(self, entries: Sequence, results: Sequence[tuple[str, FeedResult]],
              report: FetchReport,
              regions_by_entry: Optional[dict] = None) -> None:
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
        # Whether this pass saw everything a key's rate depends on. A key may only LOWER a
        # stored rate when it did: the published value is a maximum over the models sharing
        # the key and over the regions a request can reach, so a pass that missed a member,
        # missed a region, or had to widen a leg computed that maximum over less than the
        # truth. Such a pass may raise a rate and never lower one, and a genuine drop lands
        # on the next complete pass.
        #
        # Per KEY and from positive evidence per MEMBER, not from a flag for the whole pass:
        # a pagination limit in an offer that prices nothing we asked about must not freeze
        # the price of a model another feed read completely, or a safety clamp becomes a
        # permanent over-charge. `complete` starts empty and a key is complete only if every
        # one of its members produced a selection from a feed that called its own answer
        # complete.
        complete: dict[str, bool] = {}

        def _member_incomplete(model_id: str) -> bool:
            """Whether any feed that still owes an answer for this model fell short.

            A flat union across feeds was the whole of the bug: a feed lists every model it
            was asked about when it runs out of budget — honestly, since a feed that stopped
            early cannot know which of them it would have priced — so unioning froze keys
            that a different feed had read completely, and a denied Price List became a
            permanent over-charge on every Claude key.

            Ownership decides it, and ownership is not a hardcoded table: it is the same
            runtime split the rest of this module relies on. A model some feed produced a
            complete card for is that feed's, and no other feed owes an answer for it. A
            model a feed established as out of scope is not that feed's to answer. What is
            left — nobody carded it completely, and a feed that never ruled itself out fell
            short — is the case that must still freeze, because a truncated feed may be
            exactly the one that owns the model and never reached it.
            """
            carded_completely = any(
                model_id in result.cards and model_id not in result.incomplete_models
                for _, result in results
            )
            if carded_completely:
                return False
            return any(
                model_id in result.incomplete_models
                and model_id not in result.out_of_scope
                for _, result in results
            )
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
            # "A feed answered" is not "this member contributed a rate". A card that exists
            # but yields no standard-tier selection leaves the key's maximum short by that
            # member, and reading `answered` as completeness is how a $15 tier gets
            # re-published at $5.
            member_selected = False
            for name, result in results:
                card = result.cards.get(model_id)
                if not card:
                    continue
                answered = True
                regions = ((regions_by_entry or {}).get(id(entry))
                           if regions_by_entry is not None
                           else _candidate_regions(entry))
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
                member_selected = True
                current = folded.setdefault(entry.pricing_key, {})
                for field_name, token_class in _FIELD_BY_CLASS.items():
                    if token_class not in selection.rates:
                        continue
                    value = _to_micro(selection.rates[token_class])
                    if value > current.get(field_name, -1):
                        current[field_name] = value
                    live_classes.setdefault(entry.pricing_key, set()).add(token_class)
                if selection.widened:
                    complete[entry.pricing_key] = False
                    widened = report.widened.setdefault(entry.pricing_key, [])
                    for token_class in sorted(selection.widened):
                        if token_class not in widened:
                            widened.append(token_class)
                sources.setdefault(entry.pricing_key, set()).add(name)
                # Every leg, from the one leg registry. Comparing input and output alone
                # missed a key whose members diverge only on a cache rate — charged at the
                # per-leg maximum in the meantime, with nothing telling the operator the
                # tier had stopped being a tier.
                disagreement.setdefault(entry.pricing_key, {})[entry.bedrock_model_id] = tuple(
                    _to_micro(selection.rates[c]) if c in selection.rates else None
                    for c in _LEGS_IN_ORDER
                )
            # A member every feed has positively established as outside its own scope is not
            # a member this pass MISSED: nobody publishes a price for it, so the key's
            # maximum over the members that have one is complete without it. Distinguishing
            # the two is the whole reason `out_of_scope` is reported, and conflating them let
            # one unpublishable sibling freeze a key its neighbour had priced completely.
            everywhere_out_of_scope = bool(results) and all(
                model_id in result.out_of_scope for _, result in results
            )
            if everywhere_out_of_scope:
                complete.setdefault(entry.pricing_key, True)
            elif not member_selected or _member_incomplete(model_id):
                # This member did not contribute a rate, or the feed that answered for it
                # said its own answer may be partial. Either way the key's maximum was
                # computed over less than the models and regions it covers.
                complete[entry.pricing_key] = False
            else:
                complete.setdefault(entry.pricing_key, True)
            if member_selected:
                # Priced by one feed and refused by another: THIS model is not unpriced, and
                # leaving it on that list turns a report an operator acts on into noise.
                # Keyed on the model rather than on the key: a sibling sharing the key used
                # to erase this model's own notice, so "the failover set is unreadable" —
                # the one place that says so — disappeared whenever a tier had two members.
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
            key: self._complete(
                key, values, live_classes.get(key, set()),
                # Absent means no member was evaluated at all, which cannot be complete.
                may_lower=complete.get(key, False),
            )
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
                 live: set[str], *, may_lower: bool = True) -> Optional[Rate]:
        """Fill the legs no feed published from the layer underneath, or give up.

        The order is the same ladder the whole source follows — last-known-good
        snapshot, then the bundled floor for this key, then the floor's `default`.
        `None` when even that fails: a leg with no reviewed number behind it takes the
        whole key out of this source's answer rather than being charged at zero. Zero
        would turn "the provider does not publish a cache rate" into "cached tokens are
        free" — a discount nobody granted — and it is reachable, because the floor loader
        absorbs a read failure and returns nothing on a broken deploy.

        `may_lower=False` clamps every leg to at least what the layer below holds. It is
        set when this pass did not see everything the key's rate is a maximum over — a
        member of a shared key missing, a region unread, a leg widened out of scope — and
        it is the difference between "prices fell" and "we looked at less of the world".
        """
        values: dict[str, int] = {}
        for field_name, token_class in _FIELD_BY_CLASS.items():
            if token_class in live and field_name in published:
                value = published[field_name]
                if not may_lower:
                    floor_value = self._fallback_leg(key, field_name)
                    if floor_value is not None and floor_value > value:
                        value = floor_value
                values[field_name] = value
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
            # Only for a leg the stored version actually SOURCED. A leg no provider
            # publishes was filled from the floor when that version was cut, and the value
            # written is a copy of the floor as it stood that day — so serving it back
            # ranks a stale floor above the current one, and a correction to an unpublished
            # leg could never take effect. `leg_regressions` cannot notice either, because
            # the leg was never live. `live_classes` is already stored and already read on
            # load, so the fact needed to tell the two apart is to hand.
            #
            # But only where that fact EXISTS. A key the stored version records nothing
            # about — a row written by a build before this was tracked, or one whose
            # classes could not be read — is unknown, not floor-derived, and treating
            # unknown as floor-derived would drop a whole stored table to the floor on the
            # first deploy that reads it. Unknown keeps the stored value: the same rule as
            # everywhere else here, that absence falls back rather than lowering.
            if stored is not None:
                known = snapshot.live_classes.get(key)
                if known is None or _FIELD_BY_CLASS[field_name] in known:
                    return int(getattr(stored, field_name))
        floor = _floor_rates()
        rate = floor.get(key) or floor.get("default")
        return int(getattr(rate, field_name)) if rate is not None else None

    def _touch_due(self, previous: Snapshot) -> bool:
        """Whether an unchanged table is old enough to be worth confirming.

        Half the staleness window, matching the store's own throttle: the point of the
        confirmation is that the age a task reports is the age of the last CHECK, not of
        the last change.
        """
        window = stale_after_seconds() / 2.0
        seen = previous.last_seen_at or previous.fetched_at
        if not seen:
            return True
        return (self._clock() - seen) >= window

    def _maybe_persist(self, report: FetchReport, *, fenced_on: Optional[str]) -> None:
        """Store the union of what was known and what was just read.

        The union, not the fetch: a fetch that lost coverage — a renamed dimension, a
        model whose card stopped resolving — would otherwise overwrite the stored
        rate for the very keys the snapshot exists to preserve, and the next task to
        start would read the reduced table and fall to the floor for the rest. Newer
        values win per key; keys only the old snapshot had are carried forward.
        """
        with self._lock:
            previous = self._snapshot
        merged: dict[str, Rate] = dict(previous.rates) if previous else {}
        merged.update(report.rates)
        provenance: dict[str, str] = dict(previous.provenance) if previous else {}
        provenance.update(report.provenance)
        live_classes: dict[str, frozenset[str]] = (
            dict(previous.live_classes) if previous else {})
        # Union, for the reason given in `_apply`: this set is the memory of what was ever
        # published, and a set that forgets turns a frozen leg into a floor-priced one.
        for key, classes in report.live_classes.items():
            live_classes[key] = live_classes.get(key, frozenset()) | classes
        digest = digest_of(merged)
        if (previous is not None and previous.digest == digest
                and not self._touch_due(previous)):
            # Nothing to write and nothing to confirm. Gated on the store's touch window
            # rather than on full staleness: with the old gate, a table checked every hour
            # was never handed to `save()` until it had ALREADY gone stale, so the store's
            # half-window touch could not run and a healthy table reported a day's age.
            return
        if previous is not None and previous.digest != digest:
            logger.info(
                "price_table_changed",
                digest_old=previous.digest,
                digest_new=digest,
                changed_keys=sorted(_changed_keys(previous.rates, merged))[:20],
            )
        # `now` from the same clock this source measures ages with. Without it the store
        # timestamps with `time.time()` while the caller decides with an injected clock, so
        # the two disagree about whether the touch window has passed — and the disagreement
        # is invisible in production, where they happen to be the same function.
        stored = (self._store.save(merged, provenance, live_classes,
                                   now=self._clock(), fenced_on=fenced_on)
                  if self._store else None)
        if stored is not None:
            with self._lock:
                self._snapshot = stored
            return
        # Lost the fence, or could not write. Losing means another pass has already
        # published a version this one did not see, so this pass's fold is a table nothing
        # else can read — and serving it would be the pointer rewind the fence just
        # prevented, moved into memory. Adopt the winner for what is served as well as
        # what is merged from: `_merged_locked` unions `_table` OVER the snapshot, so
        # replacing only the snapshot would leave this process on its own numbers. The
        # adopted rate can be lower than this pass's, and that is not the never-lower rule
        # being broken: that rule is about what one pass saw across regions and models,
        # not about preferring the higher of two competing passes, and the stored version
        # is the rung the ladder names.
        winner = self._store.load() if self._store else None
        if winner is None:
            return
        with self._lock:
            self._snapshot = winner
            for key in winner.rates:
                self._table.pop(key, None)
                self._provenance.pop(key, None)
            self._snapshot_loaded = True

    def _observe(self, report: FetchReport) -> None:
        snapshot = self._snapshot
        previous = snapshot.rates if snapshot else {}
        confirmed = (snapshot.last_seen_at or snapshot.fetched_at) if snapshot else 0.0
        report.snapshot_age_seconds = (
            max(0.0, self._clock() - confirmed) if confirmed else None)
        regressed = sorted(set(previous) - set(report.rates)) if report.rates else []
        report.coverage_regressions = regressed
        if not report.rates and previous:
            # A pass that produced NOTHING against a table that has keys. Louder than the
            # partial case it swallows, because the ordering used to be inverted: losing
            # some keys warned, losing all of them was silent — `coverage_regressions` is
            # computed only when the pass produced rates, so the total loss emptied the one
            # list that would have said so. Charging carries on at the stored version,
            # correctly, and nothing else in this module would ever mention it again.
            # Emitted on EVERY such pass, not once per process: a feed dark for a week is
            # a repeating signal or it is not a signal at all.
            logger.warning(
                "price_feed_fetch_empty",
                stored_keys=len(previous),
                snapshot_age_seconds=(int(report.snapshot_age_seconds)
                                      if report.snapshot_age_seconds is not None else None),
                feed_errors={k: v[:2] for k, v in report.feed_errors.items()},
                note=("no feed produced a rate; charging continues at the stored version, "
                      "which is not being refreshed"),
            )
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
                           budget_seconds=report.budget_seconds,
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
# The legs in one stable order, for the per-key comparison vectors. Derived from the map
# above rather than written out again: a vector that quietly covers two of four legs is how
# a tier can stop being a tier without anything saying so.
_LEGS_IN_ORDER = tuple(_FIELD_BY_CLASS.values())


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
