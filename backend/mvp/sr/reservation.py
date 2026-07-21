"""Pool reservation token — the type that makes "no SR forward without a reserve"
a compile-time / construction-time guarantee (Fable IMPLEMENTATION_PLAN §2–§3).

Because vLLM SR is an executing gateway, Stratoclave must reserve BEFORE it
forwards. This module encodes that ordering in the type system: `forward_to_sr`
(a later substep) takes a `ConsumedProof` as its first, non-optional argument,
and a `ConsumedProof` can ONLY be produced by consuming a `PoolReservation`,
which can ONLY be minted by the reserve path. There is no public constructor —
`PoolReservation()` raises — so a forward without a prior reserve cannot be
written, not merely "should not be".

The reservation carries the `CandidatePool` snapshot (models + unit prices +
price version + pool hash), captured at reserve time. The charge is always
computed from THIS snapshot, closing the reserve→forward TOCTOU: a price/pool/
allowlist change between reserve and settle cannot move the amount, and a model
returned by SR that is not in the snapshot settles at the reserve amount
(fail-closed). `final_charge ≤ reserve_amount` holds by construction (pool-max).

STAGE S2: types only — no forward, no HTTP. `reserve_credit_for_pool` mints a
reservation over the existing atomic reserve; nothing consumes it yet. Unused on
the hot path (SR is unservable until a later substep), so behaviour is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import final


@dataclass(frozen=True)
class PricedCandidate:
    """One member of a candidate pool with its reserve-time snapshot price."""
    model_id: str
    unit_price_microusd_per_mtok: int
    price_version: str


@dataclass(frozen=True)
class CandidatePool:
    """The snapshot of `tenant_allowlist ∩ SR_backend_pool ∩ registry_priced`
    captured at reserve time. `pool_hash` lets the SR forward advertise the
    allowed set and lets settle verify the returned model was in-snapshot."""
    tenant_id: str
    models: tuple[PricedCandidate, ...]
    pool_hash: str
    snapshot_at_ms: int

    def max_unit_price(self) -> int:
        """Pool-max unit price — the reserve prices at this so an SR internal
        fallback to any pool member can never breach the reservation."""
        if not self.models:
            raise ValueError("empty candidate pool has no max price")
        return max(c.unit_price_microusd_per_mtok for c in self.models)

    def price_of(self, model_id: str) -> int | None:
        """The snapshot unit price for a model, or None if it was not in the
        snapshot (⇒ settle falls back to the reserve amount, fail-closed)."""
        for c in self.models:
            if c.model_id == model_id:
                return c.unit_price_microusd_per_mtok
        return None


class ReservationAlreadyConsumed(RuntimeError):
    """Raised if a PoolReservation is consumed twice — the single-use guard that
    (with the ledger's (reservation_id, phase) unique constraint) prevents an SR
    internal double-fire from double-charging."""


@final
class ConsumedProof:
    """Evidence that a reservation was consumed exactly once. The ONLY way to get
    one is `PoolReservation.consume()`. `forward_to_sr` (later substep) requires
    this as its first argument, so a forward without a reserve is untypable."""

    __slots__ = ("reservation_id", "pool", "max_tokens_cap", "reserve_amount_microusd")

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        raise TypeError("ConsumedProof is minted by PoolReservation.consume()")

    @classmethod
    def _mint(cls, reservation: "PoolReservation") -> "ConsumedProof":
        self = object.__new__(cls)
        object.__setattr__(self, "reservation_id", reservation.reservation_id)
        object.__setattr__(self, "pool", reservation.pool)
        object.__setattr__(self, "max_tokens_cap", reservation.max_tokens_cap)
        object.__setattr__(self, "reserve_amount_microusd",
                           reservation.reserve_amount_microusd)
        return self


@final
class PoolReservation:
    """A single-use reservation token minted only by the reserve path. Carries the
    pool snapshot + reserve amount; `consume()` yields the `ConsumedProof` the SR
    forward requires. No public constructor: `PoolReservation(...)` raises."""

    __slots__ = ("reservation_id", "pool", "max_tokens_cap",
                 "reserve_amount_microusd", "_consumed")

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        raise TypeError("use mvp.sr.reservation.reserve_credit_for_pool()")

    @classmethod
    def _mint(cls, *, reservation_id: str, pool: CandidatePool,
              max_tokens_cap: int, reserve_amount_microusd: int) -> "PoolReservation":
        self = object.__new__(cls)
        object.__setattr__(self, "reservation_id", reservation_id)
        object.__setattr__(self, "pool", pool)
        object.__setattr__(self, "max_tokens_cap", max_tokens_cap)
        object.__setattr__(self, "reserve_amount_microusd", reserve_amount_microusd)
        object.__setattr__(self, "_consumed", False)
        return self

    def consume(self) -> ConsumedProof:
        if self._consumed:
            raise ReservationAlreadyConsumed(self.reservation_id)
        object.__setattr__(self, "_consumed", True)
        return ConsumedProof._mint(self)
