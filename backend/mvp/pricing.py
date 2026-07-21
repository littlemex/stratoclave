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


logger = get_logger(__name__)

MICRO_USD_PER_USD = 1_000_000
_TOKENS_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class Rate:
    """Per-MTok rates in micro-USD for one pricing key."""

    input_per_mtok_microusd: int
    output_per_mtok_microusd: int
    cache_read_per_mtok_microusd: int
    cache_write_per_mtok_microusd: int


# Sentinel version used when NO admin override is active and charging falls back
# to the built-in `_DEFAULT_RATES`. Frozen onto the ledger so a dispute can tell
# "this was charged at the built-in defaults" apart from a real admin version.
BUILTIN_VERSION = "builtin"

# Sentinels stamped as `pricing_version` on a terminal when the charge did NOT go
# through a frozen snapshot. Each names a DISTINCT cause so a dispute / an alarm
# can tell them apart (Fable review-2 N2/N3) — never the pricing_key (bug#1).
#   UNVERSIONED_SENTINEL: an explicit caller-supplied cost, or a legacy
#     reservation that predates snapshotting — no version was in play.
#   SNAPSHOT_FAILED_SENTINEL: reserve tried to freeze a rate but the rate table
#     read failed; the settle then charged via the live-rate fallback. This is a
#     degraded path that MUST be alarmed, so it gets its own label.
UNVERSIONED_SENTINEL = "unversioned-legacy"
SNAPSHOT_FAILED_SENTINEL = "snapshot-failed"
# External authorize/capture in AMOUNT mode: the settled figure is a
# client-declared fixed amount, NOT derived from any rate version. A DISTINCT
# sentinel per cause (Fable N2/N3, authcap review-1 M-4) — a dispute must tell an
# external fixed-amount charge apart from a legacy snapshot-less inline one.
EXTERNAL_AMOUNT_SENTINEL = "external-fixed-amount"

# Version strings admins may never create (they collide with the sentinels /
# built-in tag and would make dispute labels ambiguous).
RESERVED_VERSIONS = frozenset(
    {BUILTIN_VERSION, UNVERSIONED_SENTINEL, SNAPSHOT_FAILED_SENTINEL,
     EXTERNAL_AMOUNT_SENTINEL}
)


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


# Built-in list prices (micro-USD per MTok). Sourced from published on-demand
# Bedrock/Anthropic rates. `default` is a conservative fallback (Opus-priced)
# so an unpriced model never under-charges a budget.
_DEFAULT_RATES: dict[str, Rate] = {
    "opus": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
    "sonnet": Rate(3_000_000, 15_000_000, 300_000, 3_750_000),
    "haiku": Rate(1_000_000, 5_000_000, 100_000, 1_250_000),
    # GPT-5.x on bedrock-mantle. Output priced at the Opus tier as a
    # conservative default until an admin sets an exact rate.
    "gpt-5": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
    # Self-hosted vLLM (hybrid serving). An operator-set cost-recovery rate,
    # NOT a Bedrock price. Cache rates MUST be 0 — vLLM reports no Bedrock
    # cache-token split, so any nonzero cache rate would be dead pricing that
    # also biases SAAR's warm-prefix delta (enforced by
    # models.assert_vllm_cache_rates_zero). The input/output defaults here are
    # a placeholder an operator overrides per deployment.
    "vllm": Rate(200_000, 200_000, 0, 0),
    "default": Rate(5_000_000, 25_000_000, 500_000, 6_250_000),
}


# ---------------------------------------------------------------------------
# Hot-reloaded rate cache
# ---------------------------------------------------------------------------
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
        # Serializes refreshes so concurrent requests don't stampede the table
        # or interleave a half-swapped rate map. The method really is locked now
        # (the name previously lied).
        self._lock = threading.Lock()

    def _refresh_locked(self, repo: PricingConfigRepository) -> None:
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
                self._rates = dict(_DEFAULT_RATES)
                self._version = None
                self._override_keys = frozenset()
            elif version != self._version:
                overrides = repo.load_rates(version)
                merged = dict(_DEFAULT_RATES)
                merged.update(overrides)
                self._rates = merged
                self._version = version
                self._override_keys = frozenset(overrides)
        except Exception:  # noqa: BLE001 — table missing / transient: keep last-good map.
            pass
        finally:
            self._loaded_at = time.time()

    def _ensure_fresh(self, repo: Optional[PricingConfigRepository]) -> None:
        # Double-checked under the lock: only one thread refreshes; the rest
        # either wait briefly and see the fresh map, or skip if it was just
        # loaded. A refresh failure keeps the previous map (fail-static).
        if time.time() - self._loaded_at >= _CACHE_TTL_SECONDS:
            with self._lock:
                if time.time() - self._loaded_at >= _CACHE_TTL_SECONDS:
                    self._refresh_locked(repo or PricingConfigRepository())

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

    def reset(self) -> None:
        """Test hook: drop cached state so the next get() reloads. Not locked —
        call only from single-threaded tests."""
        self._rates = dict(_DEFAULT_RATES)
        self._version = None
        self._override_keys = frozenset()
        self._loaded_at = 0.0


_cache = _RateCache()


def reset_cache() -> None:
    """Reset the module-level rate cache (used by tests)."""
    _cache.reset()


def effective_rates() -> tuple[Optional[str], dict[str, Rate], set[str]]:
    """Effective rate snapshot for the read-only admin pricing view (#66):
    (override version or None, merged defaults<-overrides map, override keys)."""
    return _cache.effective_rates()


def _mtok_cost(tokens: int, per_mtok_microusd: int) -> int:
    """Cost in micro-USD for `tokens` at a per-MTok rate, rounded up.

    Rounding up (ceil) is deliberate: a budget must never be under-charged by
    integer truncation, or a caller could nibble past a limit one sub-MTok
    request at a time.
    """
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
    warm_prefix_tokens: int = 0,
    repo: Optional[PricingConfigRepository] = None,
) -> int:
    """Up-front reservation cost in micro-USD for a request.

    Mirrors the token reservation the pipeline already computes
    (`input_estimate + max_output * effort_multiplier`) but priced per token
    type: input at the input rate, the (multiplied) max output at the output
    rate. Reasoning-effort multipliers (1/2/4/8 on the OpenAI route) apply to
    the output leg only, matching where the extra tokens are actually spent.

    SAAR (Fable design §4): ``warm_prefix_tokens`` splits the input leg — up to
    that many input tokens are expected to hit the model's warm prefix cache and
    so re-bill at the discounted ``cache_read`` rate, not the full input rate.
    The estimate for STAYING on a warm model is therefore cheaper than SWITCHING
    to a cold one (where warm=0 ⇒ every input token is full-price). ``warm=0`` (the
    default, and always in P0 until cache evidence lands) makes this byte-identical
    to the pre-SAAR estimate. Clamped so warm can never exceed the input estimate."""
    rate = _cache.get(pricing_key, repo)
    reserved_output = max(max_output_tokens, 0) * max(effort_multiplier, 1)
    total_input = max(input_tokens_est, 0)
    warm = min(max(warm_prefix_tokens, 0), total_input)
    fresh_input = total_input - warm
    return (
        _mtok_cost(fresh_input, rate.input_per_mtok_microusd)
        + _mtok_cost(warm, rate.cache_read_per_mtok_microusd)
        + _mtok_cost(reserved_output, rate.output_per_mtok_microusd)
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
    rate = _cache.get(pricing_key, repo)
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
    version, merged, override_keys = _cache.effective_rates(repo)
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
            snap = RateSnapshot(
                version=version,
                pricing_key=pricing_key,
                input_per_mtok_microusd=int(row.get("input_per_mtok_microusd", 0)),
                output_per_mtok_microusd=int(row.get("output_per_mtok_microusd", 0)),
                cache_read_per_mtok_microusd=int(
                    row.get("cache_read_per_mtok_microusd", 0)
                ),
                cache_write_per_mtok_microusd=int(
                    row.get("cache_write_per_mtok_microusd", 0)
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
    # No override: built-in default, tagged BUILTIN_VERSION.
    rate = (merged.get(pricing_key) if version else None) or _DEFAULT_RATES.get(
        pricing_key
    ) or _DEFAULT_RATES["default"]
    return _snapshot_from_rate(BUILTIN_VERSION, pricing_key, rate)


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
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> RatingRecord:
    """PURE function: rate real usage against a FROZEN snapshot (no table read).

    This is the single money computation for settle/late-settle. Same snapshot +
    same usage → same RatingRecord, so SETTLE and a reaper-race LATE_SETTLE that
    restore the same snapshot charge identically (INV-R6). ceil rounding per
    component (never under-charge by truncation).
    """
    # The snapshot froze a rounding policy; refuse to silently charge under an
    # unknown one (Fable review M4). Only ceil is implemented today — a future
    # policy would ship with its own branch AND a new pricing version.
    if snapshot.rounding != "ceil":
        raise ValueError(f"unsupported rating rounding policy: {snapshot.rounding!r}")
    comp = {}
    total = 0
    for name, tokens, rate in (
        ("input", input_tokens, snapshot.input_per_mtok_microusd),
        ("output", output_tokens, snapshot.output_per_mtok_microusd),
        ("cache_read", cache_read_tokens, snapshot.cache_read_per_mtok_microusd),
        ("cache_write", cache_write_tokens, snapshot.cache_write_per_mtok_microusd),
    ):
        t = max(int(tokens), 0)
        cost = _mtok_cost(t, int(rate))
        comp[name] = {"tokens": t, "rate_microusd_per_mtok": int(rate), "cost_microusd": cost}
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
            pc += _mtok_cost(max(int(tokens), 0), int(rate or 0))
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
