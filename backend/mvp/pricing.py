"""Dollar-denominated pricing for credit reservations.

Stratoclave's original budget unit was Bedrock tokens. Token budgets cannot
distinguish an Opus token from a Haiku token, so a per-model dollar layer sits
on top: every model maps (via `ModelEntry.pricing_key`) to a rate row, and the
credit pipeline reserves/settles in **integer micro-USD** (1 USD = 1_000_000
micro-USD). Integer math throughout — floats never touch a budget balance, so
there is no rounding drift across millions of requests.

Rates come from two places, in order:
  1. The `PricingConfig` DynamoDB table (admin-editable, hot-reloaded on a
     60-second TTL by polling only the `CURRENT` pointer item).
  2. The built-in `_DEFAULT_RATES` below, used when the table has no row for a
     pricing key. This keeps a fresh deployment costing correctly before an
     admin ever touches pricing.

Rates are quoted per million tokens (per-MTok) in micro-USD, matching how
Bedrock and Anthropic publish list prices, e.g. Opus input at $5/MTok is
5_000_000 micro-USD per MTok.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from core.logging import get_logger
from dynamo.pricing_config import PricingConfigRepository

from .rates import RATE_FIELDS, Rate as Rate, validate_rate_table


logger = get_logger(__name__)

MICRO_USD_PER_USD = 1_000_000
_TOKENS_PER_MTOK = 1_000_000


# `Rate` is re-exported from `mvp.rates` (imported above). It lives there because
# that module imports nothing from this package, so a price source can be typed and
# validated without importing the charging module; keeping it here made the source
# module reach back into a half-initialised `pricing`. Callers and tests have always
# imported it from `pricing`, so the name stays available here.


# Sentinel version used when NO admin override is active and charging falls back
# to the built-in `_DEFAULT_RATES`. Frozen onto the ledger so a dispute can tell
# "this was charged at the built-in defaults" apart from a real admin version.
BUILTIN_VERSION = "builtin"

# Sentinels stamped as `pricing_version` on a terminal when the charge did NOT go
# through a frozen snapshot. Each names a DISTINCT cause so a dispute / an alarm
# can tell them apart (Fable review-2 N2/N3) — never the pricing_key (bug#1).
#   UNVERSIONED_SENTINEL: an explicit caller-supplied cost, or a legacy
#     reservation that predates snapshotting — no version was in play.
#   SNAPSHOT_FAILED_SENTINEL: RETIRED as a label a new charge can carry. It named
#     the path where reserve failed to freeze a rate and the settle charged from
#     the live table instead — which let a rate edit between admission and settle
#     change what a request was charged. Freezing is now a precondition of
#     admission, so there is no such charge to label; the constant stays only
#     because ledger events written before that change still carry it and a
#     reader must keep recognising it. Nothing may stamp it.
UNVERSIONED_SENTINEL = "unversioned-legacy"
SNAPSHOT_FAILED_SENTINEL = "snapshot-failed"
# External authorize/capture in AMOUNT mode: the settled figure is a
# client-declared fixed amount, NOT derived from any rate version. A DISTINCT
# sentinel per cause (Fable N2/N3, authcap review-1 M-4) — a dispute must tell an
# external fixed-amount charge apart from a legacy snapshot-less inline one.
EXTERNAL_AMOUNT_SENTINEL = "external-fixed-amount"
#   PRICE_SOURCE_SENTINEL: no admin override was active, but the effective rate came
#     from a non-bundled price source (a live rate feed) rather than the built-in
#     table. Distinct from BUILTIN_VERSION so a dispute can tell "charged at the
#     shipped defaults" apart from "charged at whatever the feed said at the time";
#     the source's identity is in the logs, the rates themselves are frozen here.
PRICE_SOURCE_SENTINEL = "price-source"

# Version strings admins may never create (they collide with the sentinels /
# built-in tag and would make dispute labels ambiguous).
RESERVED_VERSIONS = frozenset(
    {BUILTIN_VERSION, UNVERSIONED_SENTINEL, SNAPSHOT_FAILED_SENTINEL,
     EXTERNAL_AMOUNT_SENTINEL, PRICE_SOURCE_SENTINEL}
)


#: Which token counts are billed, what each is billed at, and which pool of tokens
#: bounds it. ONE definition, read by both sides of the money path: `rate_usage`
#: charges from it at settle, and `mvp.reservation_bound` prices the reservation from
#: it at admission.
#:
#: This exists because the two sides used to enumerate the legs separately, and they
#: disagreed. The estimator priced three legs while the rater charged four, so a
#: request that wrote prompt cache settled above what was reserved for it — the
#: premise of the ceiling theorem, false in the shipped code. Adding a leg here is
#: now the only way to add one, and `tests/test_billable_legs_registry.py` fails the
#: build if a rate column exists without a leg or a leg without a bound group.
#:
#: `group` is the token pool whose SIZE bounds the leg at reserve time. The
#: input-side legs share one pool: the provider classifies each token it was SENT
#: into exactly one of fresh input / cache read / cache write, so their counts
#: partition the prompt (assumption 2 of the strict-mode guarantee) and one bound on
#: the total covers all three. Output is its own pool, bounded by max_output_tokens.
INPUT_SIDE = "input_side"
OUTPUT_SIDE = "output_side"


@dataclass(frozen=True)
class BillableLeg:
    """One billed token count: its name in the usage block and the rating record,
    the snapshot field holding its rate, and the token pool that bounds it."""

    name: str
    rate_field: str
    group: str


BILLABLE_LEGS: tuple["BillableLeg", ...] = (
    BillableLeg("input", "input_per_mtok_microusd", INPUT_SIDE),
    BillableLeg("cache_read", "cache_read_per_mtok_microusd", INPUT_SIDE),
    BillableLeg("cache_write", "cache_write_per_mtok_microusd", INPUT_SIDE),
    BillableLeg("output", "output_per_mtok_microusd", OUTPUT_SIDE),
)


def legs_in_group(group: str) -> tuple["BillableLeg", ...]:
    return tuple(leg for leg in BILLABLE_LEGS if leg.group == group)


def worst_rate_in_group(rates: object, group: str) -> int:
    """The highest rate any leg in `group` can bill a token at.

    Reads the legs from the registry rather than naming them, so a fourth
    input-side leg cannot be left out of the worst case — which is exactly how the
    cache-write leg came to be missing from the reservation while the settle
    charged it. Accepts anything carrying the rate fields (a live `Rate` or a
    frozen `RateSnapshot`).
    """
    return max(
        int(getattr(rates, leg.rate_field)) for leg in legs_in_group(group)
    )


def rounding_slack_microusd(group: str, tokens: int) -> int:
    """What per-leg rounding can add over a single rounding of the group total.

    `rate_usage` rounds EACH leg up; a bound that rounds the group's total once is
    not an upper bound on that sum, because ceiling is not subadditive. With all
    rates equal to 1 microUSD/MTok and one token on each of k legs, the group total
    rounds to 1 while the per-leg sum is k.

    Each nonzero leg's own ceiling adds strictly less than 1 microUSD, so n nonzero
    legs add strictly less than n, while rounding the total once already covers the
    exact value — leaving at most n-1 whole microUSD of difference.

    `tokens` caps n: `tokens` tokens cannot make more than `tokens` legs nonzero. So
    the slack is `min(legs, tokens) - 1`, floored at zero. A side with no tokens
    needs none, and a side with one token needs none either, because that token lands
    on exactly one leg whose own ceiling is the group's. Tight: the case above
    attains it.
    """
    nonzero_legs = min(len(legs_in_group(group)), max(int(tokens), 0))
    return max(nonzero_legs - 1, 0)


@dataclass(frozen=True)
class RateSnapshot:
    """The exact rate a reservation was admitted at, frozen at reserve time
    (Layer 5). Carried on the ReservationContext and serialized onto the RESERVE
    ledger event, so settle/late-settle rate the request WITHOUT re-reading the
    (possibly since-flipped) live rate table — "which price, when" is pinned.

    `rounding` is recorded so a future rounding-policy change (introduced with a
    new pricing version) never breaks the replay of a past charge.

    `cost_*` are the optional provider-cost rates (Layer 5 cost passthrough). They
    are nullable and RECORD-ONLY — they never affect the charged amount — but the
    columns exist in the snapshot from day one because ledger terminals are
    append-only and a cost field cannot be backfilled later.
    """

    version: str
    pricing_key: str
    input_per_mtok_microusd: int
    output_per_mtok_microusd: int
    cache_read_per_mtok_microusd: int
    cache_write_per_mtok_microusd: int
    rounding: str = "ceil"
    cost_input_per_mtok_microusd: Optional[int] = None
    cost_output_per_mtok_microusd: Optional[int] = None
    cost_cache_read_per_mtok_microusd: Optional[int] = None
    cost_cache_write_per_mtok_microusd: Optional[int] = None

    def to_ledger_dict(self) -> dict:
        """Compact, self-describing serialization for the RESERVE ledger event.

        Only non-null fields are emitted (DynamoDB forbids null attribute
        values); `from_ledger_dict` restores the same snapshot."""
        d = {
            "version": self.version,
            "pricing_key": self.pricing_key,
            "input": self.input_per_mtok_microusd,
            "output": self.output_per_mtok_microusd,
            "cache_read": self.cache_read_per_mtok_microusd,
            "cache_write": self.cache_write_per_mtok_microusd,
            "rounding": self.rounding,
        }
        for k, v in (
            ("cost_input", self.cost_input_per_mtok_microusd),
            ("cost_output", self.cost_output_per_mtok_microusd),
            ("cost_cache_read", self.cost_cache_read_per_mtok_microusd),
            ("cost_cache_write", self.cost_cache_write_per_mtok_microusd),
        ):
            if v is not None:
                d[k] = int(v)
        return d

    @classmethod
    def from_ledger_dict(cls, d: dict) -> "RateSnapshot":
        def _opt(key):
            v = d.get(key)
            return int(v) if v is not None else None

        return cls(
            version=str(d["version"]),
            pricing_key=str(d["pricing_key"]),
            input_per_mtok_microusd=int(d["input"]),
            output_per_mtok_microusd=int(d["output"]),
            cache_read_per_mtok_microusd=int(d["cache_read"]),
            cache_write_per_mtok_microusd=int(d["cache_write"]),
            rounding=str(d.get("rounding", "ceil")),
            cost_input_per_mtok_microusd=_opt("cost_input"),
            cost_output_per_mtok_microusd=_opt("cost_output"),
            cost_cache_read_per_mtok_microusd=_opt("cost_cache_read"),
            cost_cache_write_per_mtok_microusd=_opt("cost_cache_write"),
        )


@dataclass(frozen=True)
class RatingRecord:
    """The frozen money breakdown for one settle, embedded on the ledger terminal.

    Self-contained (INV-R2): `recompute(tokens × rate) == total` is verifiable
    from this record alone, with no external table read. `total_cost_microusd` is
    the SINGLE source of the settled amount — settle uses THIS value, so the
    ledger's settled_delta and this record can never disagree.

    `provider_cost_microusd` / `margin_microusd` are populated only when the
    snapshot carried cost rates; they are record-only and never affect `total`.
    """

    pricing_version: str
    pricing_key: str
    rounding: str
    # per-component: (tokens, rate_per_mtok_microusd, cost_microusd)
    components: dict
    total_cost_microusd: int
    provider_cost_microusd: Optional[int] = None
    margin_microusd: Optional[int] = None

    def to_ledger_dict(self) -> dict:
        d = {
            "pricing_version": self.pricing_version,
            "pricing_key": self.pricing_key,
            "rounding": self.rounding,
            "components": self.components,
            "total_cost_microusd": int(self.total_cost_microusd),
        }
        if self.provider_cost_microusd is not None:
            d["provider_cost_microusd"] = int(self.provider_cost_microusd)
        if self.margin_microusd is not None:
            d["margin_microusd"] = int(self.margin_microusd)
        return d


# The bundled rate document is the FLOOR: read from disk, no network, no
# credentials, so it cannot fail at runtime. `_DEFAULT_RATES` keeps its name and
# role — the map every other layer degrades to — but its values now live in
# defaults/pricing.json so an operator edits data, not code. See mvp/price_sources.py
# for the three-layer resolution (floor -> active source -> admin overrides).
def _load_floor_rates() -> dict[str, Rate]:
    from .price_sources import load_rate_document

    return load_rate_document()


_DEFAULT_RATES: dict[str, Rate] = _load_floor_rates()


def baseline_rates() -> dict[str, Rate]:
    """A copy of the built-in floor. Public so callers that need a price-derived
    fact (routing cost tiers, for one) do not reach for a private map."""
    return dict(_DEFAULT_RATES)


_CACHE_TTL_SECONDS = 60.0


class _RateCache:
    """Process-local cache of the effective rate table.

    Holds the merged (defaults <- table overrides) rate map plus the pricing
    version string the overrides were loaded at. Refreshes when the TTL
    lapses; a refresh reads only the small `CURRENT` pointer, and pulls the
    full override set only when the version changed.
    """

    def __init__(self) -> None:
        self._rates: dict[str, Rate] = dict(_DEFAULT_RATES)
        self._version: Optional[str] = None
        # Which keys in `_rates` came from the DynamoDB override set (vs a
        # built-in default). Kept so the read-only pricing view (#66) can tell
        # an operator what's customized without re-reading the table.
        self._override_keys: frozenset[str] = frozenset()
        self._loaded_at: float = 0.0
        # Overrides kept verbatim so a refresh can re-merge them over a fresh source
        # table without re-reading the table when the version has not moved.
        self._overrides: dict[str, Rate] = {}
        # Last table the active source returned. Held so a later failure keeps
        # charging at real prices instead of regressing to the bundled floor.
        self._source_table: Optional[dict[str, Rate]] = None
        # Which keys the active source actually supplied. The snapshot label needs
        # per-key provenance: "the source is configured" is not the same fact as
        # "this key's rate came from it".
        self._source_keys: frozenset[str] = frozenset()
        # Events already logged, so a standing condition does not repeat every refresh.
        self._warned: set[str] = set()
        # Serializes refreshes so concurrent requests don't stampede the table
        # or interleave a half-swapped rate map. The method really is locked now
        # (the name previously lied).
        self._lock = threading.Lock()

    def _baseline(self) -> dict[str, Rate]:
        """The active price source's table, layered over the bundled floor.

        Three failure modes, deliberately not the same:

        - the source NAME is unknown: a configuration error, so it propagates.
          Charging the floor because someone typo'd `STRATOCLAVE_PRICE_SOURCE` is a
          billing error, not something to log and continue through.
        - `load()` raises or returns something malformed: transient or a bad
          deployment of a source, so the LAST GOOD table stays in force. Falling
          back to the floor here would be worse than doing nothing — a source that
          supplies higher real-world prices than the bundled defaults would start
          under-charging the moment it blipped.
        - it has never succeeded: the floor, which is the only table guaranteed to
          exist.

        The floor is merged underneath either way, so a source that omits keys
        cannot make a model unpriceable. `default` is the one key a source may not
        lower: it is the rate an unregistered pricing key is charged at, and the
        standing rule is that an unpriced model over-charges rather than under.
        """
        table = self._source_table if self._source_table is not None else {}

        merged = dict(_DEFAULT_RATES)
        merged.update(table)
        self._source_keys = frozenset(table)
        merged["default"] = self._floored_default(merged["default"])
        return merged

    def _floored_default(self, candidate: Rate) -> Rate:
        """`default` is what an UNREGISTERED pricing key is charged at, so it may only
        move up. Clamped per field, not judged on one of them: a source that matched
        the floor's output rate while lowering input or cache rates would otherwise
        slip past and under-charge every unpriced model.
        """
        floor = _DEFAULT_RATES["default"]
        clamped = Rate(**{
            field: max(getattr(candidate, field), getattr(floor, field))
            for field in RATE_FIELDS
        })
        if clamped != candidate:
            self._warn_once(
                "price_source_default_key_floored",
                note="a source may not lower any field of the 'default' rate",
            )
        else:
            self._clear_warning("price_source_default_key_floored")
        return clamped

    def _with_vllm_cache_rates_zeroed(self, merged: dict[str, Rate]) -> dict[str, Rate]:
        """vLLM reports no Bedrock cache-token split, so a nonzero cache rate on a
        key a vLLM entry uses is dead pricing that also skews SAAR's warm-prefix
        delta. The registry enforces this for the bundled floor, but a source is
        re-read on every refresh and could reintroduce it after that check, so it is
        clamped here — on the money path, every refresh.
        """
        from .models import registry_entries

        keys = {e.pricing_key for e in registry_entries()
                if getattr(e, "served_by", "bedrock") == "vllm"}
        clamped_any = False
        for key in keys & set(merged):
            rate = merged[key]
            if rate.cache_read_per_mtok_microusd or rate.cache_write_per_mtok_microusd:
                clamped_any = True
                self._warn_once(
                    "vllm_cache_rate_zeroed",
                    note=f"pricing key {key!r} is used by a vLLM entry and must price cache at 0",
                )
                merged[key] = Rate(
                    rate.input_per_mtok_microusd, rate.output_per_mtok_microusd, 0, 0,
                )
        if not clamped_any:
            self._clear_warning("vllm_cache_rate_zeroed")
        return merged

    def _warn_once(self, event: str, **fields) -> None:
        """Log a persistent condition on transition, not on every 60 s refresh."""
        if event in self._warned:
            return
        self._warned.add(event)
        logger.warning(event, source=self._active_source_name(), **fields)

    def _clear_warning(self, event: str) -> None:
        """Arm `_warn_once` again for `event`.

        Suppressing a standing condition and detecting its recurrence are different
        jobs: without this, a source that lowered a rate once, was fixed, and did it
        again months later would never be logged a second time.
        """
        self._warned.discard(event)

    @staticmethod
    def _active_source_name() -> str:
        from .price_sources import active_source_name

        return active_source_name()

    def _refresh_locked(self, repo: PricingConfigRepository) -> None:
        from dynamo.pricing_config import RateDocumentInvalid

        from .price_sources import PriceSourceConfigError

        # Fail-static across the ENTIRE refresh (Fable #66 rev1 BUG1): a throw
        # from EITHER current_version() OR load_rates() (throttle, transient
        # Dynamo error, malformed item) must keep the previous map and bump
        # _loaded_at — otherwise a table blip both breaks the live charging path
        # (get() would raise) AND stampedes the failing read on every call.
        try:
            version = repo.current_version()
            if version is None:
                # Overrides removed (CURRENT pointer gone). Fall back to built-in
                # defaults rather than keeping the last-loaded set alive forever.
                self._rates = self._with_vllm_cache_rates_zeroed(self._baseline())
                self._version = None
                self._overrides = {}
                self._override_keys = frozenset()
            else:
                # Re-read the source on EVERY refresh, not only when the override
                # version moves. Gating it on the version froze live prices for as
                # long as an operator left the override set alone — the opposite of
                # what "refreshed on the cache interval" promises.
                overrides = (
                    repo.load_rates(version) if version != self._version
                    else dict(self._overrides)
                )
                merged = self._baseline()
                merged.update(overrides)
                # AFTER the overrides, not before: an admin row can reintroduce a
                # nonzero cache rate that the source clamp already removed, and the
                # invariant is about what is actually charged.
                merged = self._with_vllm_cache_rates_zeroed(merged)
                self._rates = merged
                self._version = version
                self._overrides = dict(overrides)
                self._override_keys = frozenset(overrides)
        except (PriceSourceConfigError, RateDocumentInvalid):
            # Neither is a transient. A misconfigured price source and an invalid
            # rate document will both still be invalid on the next tick, so absorbing
            # them here would charge the bundled floor for as long as nobody reads
            # the logs — the silent fallback this module forbids. Re-raised BEFORE
            # the catch-all because both are ValueErrors and would otherwise be
            # swallowed by it; the reserve path turns them into a refusal.
            raise
        except Exception:  # noqa: BLE001 — table missing / transient: keep last-good map.
            pass
        except PriceSourceConfigError:
            # Deliberately BEFORE the timestamp update below: advancing it would let
            # the next 60 s of requests skip the refresh entirely and charge whatever
            # table is currently held — the initial floor on a cold cache. One request
            # erroring per interval while the rest silently under-charge is the same
            # silent fallback, moved onto the time axis.
            raise
        else:
            self._loaded_at = time.time()

    def _ensure_fresh(self, repo: Optional[PricingConfigRepository]) -> None:
        # Double-checked under the lock: only one thread refreshes; the rest
        # either wait briefly and see the fresh map, or skip if it was just
        # loaded. A refresh failure keeps the previous map (fail-static).
        if time.time() - self._loaded_at >= _CACHE_TTL_SECONDS:
            # The price source is fetched OUTSIDE the lock. A live feed can be slow,
            # and holding the lock across it would stall every concurrent get() and
            # snapshot read behind one network call — turning "the source is never on
            # the request path" into "every request waits for it". Readers keep
            # serving the previous table while this runs.
            self._prefetch_source()
            with self._lock:
                if time.time() - self._loaded_at >= _CACHE_TTL_SECONDS:
                    self._refresh_locked(repo or PricingConfigRepository())

    def _prefetch_source(self) -> None:
        """Refresh `_source_table` from the active source, off the lock.

        A configuration error propagates (see `_baseline`); a load failure leaves the
        last good table in place and is logged. Only the assignment touches shared
        state, and it is a single reference swap.
        """
        from .price_sources import PriceSourceConfigError, load_from_active_source

        try:
            self._source_table = load_from_active_source()
            self._clear_warning("price_source_load_failed")
        except PriceSourceConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 — transient: keep the last good table.
            self._warn_once(
                "price_source_load_failed",
                error=str(exc),
                note=("keeping the last good source table"
                      if self._source_table is not None else
                      "no source table has ever loaded; using the bundled floor"),
            )

    def get(self, pricing_key: str, repo: Optional[PricingConfigRepository] = None) -> Rate:
        self._ensure_fresh(repo)
        rates = self._rates
        return rates.get(pricing_key) or rates.get("default") or _DEFAULT_RATES["default"]

    def effective_rates(
        self, repo: Optional[PricingConfigRepository] = None
    ) -> tuple[Optional[str], dict[str, Rate], set[str]]:
        """One-shot snapshot: (version, merged rate map, keys that are overrides).

        Rides the SAME refresh path as get() so the read-only view can never
        diverge from what pricing actually charges."""
        self._ensure_fresh(repo)
        # Read the three fields UNDER THE LOCK (Fable #66 rev1 BUG2): a
        # concurrent refresh assigns them on separate lines, so an unlocked read
        # could mix generations (rates from vN+1 with override_keys from vN ->
        # mislabeled source). Snapshotting under the lock keeps them consistent.
        with self._lock:
            return self._version, dict(self._rates), set(self._override_keys)

    def snapshot_inputs(
        self, repo: Optional[PricingConfigRepository] = None
    ) -> tuple[Optional[str], dict[str, Rate], set[str], frozenset[str]]:
        """`(version, rates, override_keys, source_keys)` read in ONE locked pass.

        The refresh assigns those fields on separate lines, so reading them in
        separate locked calls can mix generations — a rate from the new table with a
        provenance label from the old one. The amount charged would still be
        self-consistent (reserve and settle share the frozen snapshot), but the
        dispute label would describe a table that never priced it.
        """
        self._ensure_fresh(repo)
        with self._lock:
            return (self._version, dict(self._rates), set(self._override_keys),
                    self._source_keys)

    @staticmethod
    def _tag_for(pricing_key: str, source_keys: frozenset[str]) -> str:
        from .price_sources import DEFAULT_SOURCE_NAME, active_source_name

        if active_source_name() == DEFAULT_SOURCE_NAME:
            return BUILTIN_VERSION
        return PRICE_SOURCE_SENTINEL if pricing_key in source_keys else BUILTIN_VERSION

    def reset(self) -> None:
        """Test hook: drop cached state so the next get() reloads. Not locked —
        call only from single-threaded tests."""
        self._rates = dict(_DEFAULT_RATES)
        self._version = None
        self._overrides = {}
        self._override_keys = frozenset()
        self._source_table = None
        self._source_keys = frozenset()
        self._warned = set()
        self._loaded_at = 0.0


_cache = _RateCache()


def reset_cache() -> None:
    """Reset the module-level rate cache (used by tests).

    Also drops the memoised routing cost tiers. NOTE: the bundled floor itself is an
    import-time snapshot and is NOT re-read, so swapping STRATOCLAVE_PRICING_PATH
    after import changes what the active source returns but not the floor.
    """
    _cache.reset()
    try:
        from .routing.chains import _tier_for

        _tier_for.cache_clear()
    except Exception:  # noqa: BLE001 — a test-only convenience, never load-bearing.
        pass


def effective_rates() -> tuple[Optional[str], dict[str, Rate], set[str]]:
    """Effective rate snapshot for the read-only admin pricing view (#66):
    (override version or None, merged defaults<-overrides map, override keys)."""
    return _cache.effective_rates()


def rate_for(pricing_key: str, repo: Optional[PricingConfigRepository] = None) -> Rate:
    """The LIVE effective `Rate` for `pricing_key` (defaults<-overrides, TTL-cached).

    Public wrapper around the process-local `_cache.get()` so a caller that needs
    a rate (the hard-ceiling reservation bound in `reservation_bound.py`, for one)
    does not reach into the private cache object. Deliberately the SAME live read
    `estimate_cost_microusd` uses, not the frozen `RateSnapshot` — the admission
    bound is computed at the same "now" as the legacy estimate it replaces, and
    the reserve chokepoint freezes its own snapshot moments later for settle. The
    tiny window between the two reads already existed for `estimate_cost_microusd`
    and is unrelated to the soundness of the *bound* itself (the bound is sound
    for whatever rate it prices at, live or frozen).
    """
    return _cache.get(pricing_key, repo)


def _mtok_cost(tokens: int, per_mtok_microusd: int) -> int:
    """Cost in micro-USD for `tokens` at a per-MTok rate, rounded up.

    Rounding up (ceil) is deliberate: a budget must never be under-charged by
    integer truncation, or a caller could nibble past a limit one sub-MTok
    request at a time.

    A negative rate is refused rather than computed. Ceil rounding and integer
    micro-USD are the mechanisms behind "a request is never under-charged", and a
    negative leg defeats both by minting credit: `_mtok_cost(1000, -5_000_000)`
    used to return −5,000 and the ledger recorded it as truth. The defence was
    that no rate document had ever held one, which is a discipline, not a
    mechanism. Refusing here makes it a mechanism on every charging path,
    whatever wrote the document.
    """
    if per_mtok_microusd < 0:
        raise ValueError(
            f"negative rate: {per_mtok_microusd} micro-USD per MTok. A rate that "
            "credits an account is not a price; reject the rate document."
        )
    if tokens <= 0:
        return 0
    numerator = tokens * per_mtok_microusd
    return -(-numerator // _TOKENS_PER_MTOK)  # ceil division


def mtok_cost_for_rounding(tokens: int, per_mtok_microusd: int, rounding: str) -> int:
    """Public rounding-aware component cost, used by the ledger's rating replay
    audit to RE-COMPUTE (not just re-sum) a frozen rating under its own frozen
    rounding policy. Only `ceil` is defined today; an unknown policy raises so a
    replay can never silently 'pass' a rating written under a policy this code
    does not understand."""
    if rounding != "ceil":
        raise ValueError(f"unsupported rating rounding policy: {rounding!r}")
    return _mtok_cost(tokens, per_mtok_microusd)


def estimate_cost_microusd(
    *,
    pricing_key: str,
    input_tokens_est: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """Up-front reservation cost in micro-USD for a request.

    Mirrors the token reservation the pipeline already computes
    (`input_estimate + max_output * effort_multiplier`) but priced per token
    type: input at the input rate, the (multiplied) max output at the output
    rate. Reasoning-effort multipliers (1/2/4/8 on the OpenAI route) apply to
    the output leg only, matching where the extra tokens are actually spent.

    Every input-side token is priced at the WORST rate any input-side leg can bill it
    at, read from the same leg registry the settle rater charges from. The gateway
    does not decide which leg a token lands on — the provider does, and it reports
    that afterwards — so pricing the estimate at the input rate reserved below the
    charge for any request that wrote prompt cache: 7,500 microUSD reserved against
    38,750 settled, on the success path, with nothing to notice.

    What this does NOT establish is premise (P) of the ceiling theorem. This function
    prices an ESTIMATED token count, and an estimate is not a bound: a prompt that
    tokenises to more than ``input_tokens_est`` still settles above its reservation.
    Only `mvp.reservation_bound`, which prices a ceiling on the token count rather
    than a guess, carries the ceiling claim — see `dollar_pool_bound_state`.

    This function also does not take cache evidence. It used to: SAAR passed
    ``warm_prefix_tokens`` and that many input tokens were re-priced at the cheaper
    ``cache_read`` rate, so staying on a warm model reserved LESS than switching to a
    cold one. A discount applied at reserve time to a leg the provider has not yet
    chosen is a reservation below the possible charge, which is not a reservation.
    The warm preference belongs in which candidate is CHOSEN, and the money claim
    about it belongs in `switch_cost_delta_microusd` — a comparison recorded on the
    decision, not an amount that gates admission.

    This wrapper reads the LIVE table and is for callers that are not admitting a
    request (a comparison, an operator view). The admission path must call
    `estimate_cost_from_rates` with the snapshot it froze, so the amount that gates
    admission and the amount the settle charges cannot come from two different
    documents — see that function."""
    return estimate_cost_from_rates(
        _cache.get(pricing_key, repo),
        input_tokens_est=input_tokens_est,
        max_output_tokens=max_output_tokens,
        effort_multiplier=effort_multiplier,
    )


def estimate_cost_from_rates(
    rates: object,
    *,
    input_tokens_est: int,
    max_output_tokens: int,
    effort_multiplier: int = 1,
) -> int:
    """`estimate_cost_microusd`'s arithmetic over rates the caller already holds.

    `rates` is any object carrying the four per-MTok fields — a `Rate` or a frozen
    `RateSnapshot` — which is the whole point: the reserve chokepoint freezes a
    snapshot and prices the admission from THAT object, so the price that admits a
    request and the price that charges it are one document by construction. Pricing
    the admission with a separate live read left a window in which a rate refresh
    landing between the two reads sized the reservation at one rate and charged it
    at another, and recorded, as "the version that priced this admission", a
    version that priced nothing.
    """
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    total_input = max(input_tokens_est, 0)
    return (
        _mtok_cost(total_input, worst_rate_in_group(rates, INPUT_SIDE))
        + _mtok_cost(reserved_output, rates.output_per_mtok_microusd)
        + rounding_slack_microusd(INPUT_SIDE, total_input)
        + rounding_slack_microusd(OUTPUT_SIDE, reserved_output)
    )


def switch_cost_delta_microusd(
    *,
    pricing_key: str,
    warm_prefix_tokens: int,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """The micro-USD *penalty* of discarding a warm prefix cache by switching
    models (a "cache checkout"). If the session stays on its warm model, the
    ``warm_prefix_tokens`` re-bill at the discounted cache-read rate; if it
    switches, that same prefix is cold on the new model and re-bills at the full
    input rate. The delta a switch costs is therefore:

        warm_prefix_tokens × (input_rate − cache_read_rate)

    priced at ``pricing_key``'s current rate. Non-negative by construction (the
    cache-read rate is never above the input rate); clamped at 0 defensively so a
    misconfigured rate table can never turn a switch into a fake saving.

    SOURCE-AGNOSTIC (SR migration §S1-3): this is a pure ledger-side pricing
    primitive. It takes only a `warm_prefix_tokens` hint and does NOT depend on
    who supplied it — the legacy self-hosted SAAR router, or a future vLLM
    Semantic Router decision. The reserve path adds this to a switch candidate's
    expected cost and records it as the provable claim, computed from the same
    versioned rate table the ledger charges from, so a replay recomputes it
    exactly (Fable SAAR design §4)."""
    rate = _cache.get(pricing_key, repo)
    per_mtok = max(0, rate.input_per_mtok_microusd - rate.cache_read_per_mtok_microusd)
    return _mtok_cost(max(warm_prefix_tokens, 0), per_mtok)


# Deprecated alias (SR migration §S1-3): the old SAAR-specific name is kept so
# existing callers/tests/specs stay green while the rename lands incrementally.
# Remove in stage 2 once all call sites reference the source-agnostic name.
saar_checkout_delta_microusd = switch_cost_delta_microusd


def actual_cost_microusd(
    *,
    pricing_key: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """Settled cost in micro-USD from a response's real usage block.

    DEPRECATED for the charging path (Layer 5): re-reads the live rate table, so
    a rate flip between reserve and settle would charge at the wrong price. Use
    `snapshot_rates()` at reserve + `rate_usage(snapshot, usage)` at settle so the
    charge is pinned to the admitted version. Kept for callers that only need a
    quick estimate and do not span a reserve→settle boundary.
    """
    return actual_cost_from_rate(
        _cache.get(pricing_key, repo),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def actual_cost_from_rate(
    rate: object,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> int:
    """Settled cost from real usage at a rate the caller already holds.

    The counterpart of `estimate_cost_from_rates` on the settle side, and the one
    entry point that lets a caller price at a rate it can name: a report that has
    to be reproducible embeds the rates it used and prices through here, instead of
    re-reading a table that has since moved. `actual_cost_microusd` is this
    function over a live read, so the two can never diverge in arithmetic.
    """
    return (
        _mtok_cost(max(input_tokens, 0), rate.input_per_mtok_microusd)
        + _mtok_cost(max(output_tokens, 0), rate.output_per_mtok_microusd)
        + _mtok_cost(max(cache_read_tokens, 0), rate.cache_read_per_mtok_microusd)
        + _mtok_cost(max(cache_write_tokens, 0), rate.cache_write_per_mtok_microusd)
    )


# ---------------------------------------------------------------------------
# Layer 5 rating: freeze at reserve, charge from the frozen snapshot
# ---------------------------------------------------------------------------

# Immutable per-(version, pricing_key) cache. Version rows never change after
# `set_rates` flips CURRENT, so once read a row is cached forever (no TTL). This
# keeps settle's rating a pure function with no live-table dependency.
_version_rate_cache: dict[tuple, RateSnapshot] = {}
_version_cache_lock = threading.Lock()


def snapshot_rates(
    pricing_key: str, repo: Optional[PricingConfigRepository] = None
) -> RateSnapshot:
    """Freeze the effective rate for `pricing_key` at THIS moment (reserve time).

    Reads the active version (via the existing 60s CURRENT cache) and the exact
    rate row for that version, and returns an immutable RateSnapshot to carry
    through to settle. When no admin override is active for the key, the snapshot
    is the built-in default tagged `BUILTIN_VERSION`.

    INV-R4 (version internal consistency): `set_rates` writes all of a version's
    rows BEFORE flipping CURRENT, and rows are immutable after — so reading
    CURRENT then the row can never mix two versions, even if a flip races in
    between (we read the row for the version CURRENT named, which is fully
    written and frozen).
    """
    version, merged, override_keys, source_keys = _cache.snapshot_inputs(repo)
    if version is not None and pricing_key in override_keys:
        # An admin override is active for this key: freeze the versioned row's
        # exact values (+ any cost_* fields) from the immutable per-version cache.
        ck = (version, pricing_key)
        cached = _version_rate_cache.get(ck)
        if cached is not None:
            return cached
        row = (repo or PricingConfigRepository()).get_rates_for_version(
            version, pricing_key
        )
        if row is not None:
            # Defensive cross-check (Fable review-2 N4): the composite sort key
            # `__ratever__<version>__<key>` could in principle be reached by a
            # different (version, key) split. set_rates forbids the delimiters
            # that allow it, but confirm the row's own fields match what we asked
            # for so a mis-keyed row can never be silently rated.
            if str(row.get("version")) != version or str(row.get("pricing_key")) != pricing_key:
                raise RuntimeError(
                    f"pricing row key mismatch: asked ({version!r},{pricing_key!r}) "
                    f"got ({row.get('version')!r},{row.get('pricing_key')!r})"
                )
            # Validate the row this snapshot is built from, with the same rule the
            # bulk load applies. A `.get(leg, 0)` here substituted a rate of ZERO
            # for a leg the document does not carry — the one place where a partial
            # or hand-written row becomes the price a request is admitted and
            # charged at, and the boundary `load_rates` validates does not cover it
            # because the snapshot is a point read.
            from dynamo.pricing_config import validate_rate_row_legs

            validate_rate_row_legs(row, version=version, pricing_key=pricing_key)
            snap = RateSnapshot(
                version=version,
                pricing_key=pricing_key,
                input_per_mtok_microusd=int(row["input_per_mtok_microusd"]),
                output_per_mtok_microusd=int(row["output_per_mtok_microusd"]),
                cache_read_per_mtok_microusd=int(
                    row["cache_read_per_mtok_microusd"]
                ),
                cache_write_per_mtok_microusd=int(
                    row["cache_write_per_mtok_microusd"]
                ),
                cost_input_per_mtok_microusd=_opt_int(row.get("cost_input_per_mtok_microusd")),
                cost_output_per_mtok_microusd=_opt_int(row.get("cost_output_per_mtok_microusd")),
                cost_cache_read_per_mtok_microusd=_opt_int(
                    row.get("cost_cache_read_per_mtok_microusd")
                ),
                cost_cache_write_per_mtok_microusd=_opt_int(
                    row.get("cost_cache_write_per_mtok_microusd")
                ),
            )
            with _version_cache_lock:
                _version_rate_cache[ck] = snap
            return snap
        # Row missing for an ACTIVE override under a strongly-consistent read is
        # a real inconsistency, not a normal case. Do NOT tag a fallback rate with
        # the real version (that would be a false dispute label — Fable review-2
        # N5, the M1 class again). Raise: the reserve caller catches it, marks the
        # reservation `snapshot-failed`, and the honest sentinel is stamped.
        raise RuntimeError(
            f"pricing version {version!r} is active but its {pricing_key!r} row "
            f"is missing (consistency violation)"
        )
    # No admin override. The rate still comes from the EFFECTIVE table, which
    # includes whatever the active price source supplied — reading the bundled floor
    # here instead would freeze a different rate than the one the reservation was
    # admitted at, and a dearer feed would then settle below what it reserved.
    # `merged` already contains the floor, so it is the single place to read: taking
    # the floor's `default` when the key is unknown would freeze a different rate
    # than the reservation was admitted at, which is the same under-charge shape as
    # reading the floor instead of the source.
    rate = merged.get(pricing_key)
    # An unknown key is charged at `default`, so the dispute label must describe
    # where `default` came from — labelling it by the unknown key would read as
    # "charged at the shipped defaults" while actually charging a feed's rate.
    label_key = pricing_key if rate is not None else "default"
    if rate is None:
        rate = merged["default"]
    return _snapshot_from_rate(_cache._tag_for(label_key, source_keys), pricing_key, rate)


def _opt_int(v):
    return int(v) if v is not None else None


def _snapshot_from_rate(version: str, pricing_key: str, rate: Rate) -> RateSnapshot:
    return RateSnapshot(
        version=version,
        pricing_key=pricing_key,
        input_per_mtok_microusd=rate.input_per_mtok_microusd,
        output_per_mtok_microusd=rate.output_per_mtok_microusd,
        cache_read_per_mtok_microusd=rate.cache_read_per_mtok_microusd,
        cache_write_per_mtok_microusd=rate.cache_write_per_mtok_microusd,
    )


def rate_usage(
    snapshot: RateSnapshot,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: Optional[int] = 0,
    cache_write_tokens: Optional[int] = 0,
) -> RatingRecord:
    """PURE function: rate real usage against a FROZEN snapshot (no table read).

    This is the single money computation for settle/late-settle. Same snapshot +
    same usage → same RatingRecord, so SETTLE and a reaper-race LATE_SETTLE that
    restore the same snapshot charge identically (INV-R6). ceil rounding per
    component (never under-charge by truncation).

    A token count of `None` means the provider did not report that leg — some
    models never report prompt-cache counts at all. It costs the same as zero,
    because there is nothing to charge, but the component records
    `reported: False` so the ledger does not assert a measurement nobody made. A
    reader comparing models needs "not reported" and "reported as none" to be
    different facts; they are the difference between "this model does not cache"
    and "this model did not cache this time".
    """
    # The snapshot froze a rounding policy; refuse to silently charge under an
    # unknown one (Fable review M4). Only ceil is implemented today — a future
    # policy would ship with its own branch AND a new pricing version.
    if snapshot.rounding != "ceil":
        raise ValueError(f"unsupported rating rounding policy: {snapshot.rounding!r}")
    # Iterate the ONE leg registry rather than a second literal list. The list that
    # used to live here was the settle side of a disagreement: the estimator had its
    # own, shorter one, and the missing cache-write leg is what broke the ceiling.
    observed = {
        "input": input_tokens,
        "output": output_tokens,
        "cache_read": cache_read_tokens,
        "cache_write": cache_write_tokens,
    }
    comp = {}
    total = 0
    for name, tokens, rate in (
        (leg.name, observed[leg.name], getattr(snapshot, leg.rate_field))
        for leg in BILLABLE_LEGS
    ):
        reported = tokens is not None
        t = max(int(tokens or 0), 0)
        cost = _mtok_cost(t, int(rate))
        comp[name] = {
            "tokens": t,
            "rate_microusd_per_mtok": int(rate),
            "cost_microusd": cost,
            # Whether the PROVIDER reported this leg. `tokens: 0` alone cannot say
            # it: an absent count and a reported zero are different facts and only
            # one of them is a measurement.
            "reported": reported,
        }
        total += cost

    provider_cost = None
    margin = None
    cost_rates = (
        snapshot.cost_input_per_mtok_microusd,
        snapshot.cost_output_per_mtok_microusd,
        snapshot.cost_cache_read_per_mtok_microusd,
        snapshot.cost_cache_write_per_mtok_microusd,
    )
    if any(r is not None for r in cost_rates):
        pc = 0
        for tokens, rate in (
            (input_tokens, snapshot.cost_input_per_mtok_microusd),
            (output_tokens, snapshot.cost_output_per_mtok_microusd),
            (cache_read_tokens, snapshot.cost_cache_read_per_mtok_microusd),
            (cache_write_tokens, snapshot.cost_cache_write_per_mtok_microusd),
        ):
            pc += _mtok_cost(max(int(tokens or 0), 0), int(rate or 0))
        provider_cost = pc
        margin = total - pc

    return RatingRecord(
        pricing_version=snapshot.version,
        pricing_key=snapshot.pricing_key,
        rounding=snapshot.rounding,
        components=comp,
        total_cost_microusd=total,
        provider_cost_microusd=provider_cost,
        margin_microusd=margin,
    )


def reset_version_cache() -> None:
    """Test hook: clear the immutable per-version snapshot cache."""
    with _version_cache_lock:
        _version_rate_cache.clear()
