"""S7 observability: SR-vs-ledger divergence + the decision-log evidence block
(Fable IMPLEMENTATION_PLAN §5-§7, and the master's original ask — join SR's
observability to Stratoclave's ledger).

The receptacle already exists: decision_log.build_decision_item(vsr=...) stores an
open dict keyed by (run_id, span_id). SR rides it by adding keys — no schema
change. The join key is span_id ↔ SR's x-vsr-replay-id.

The one number that is charge-of-record is the LEDGER's. SR's own cost is
EVIDENCE. This module computes the divergence between them so a persistent gap is
alarmable (a stale price table, an SR pricing drift, or a token-count mismatch),
WITHOUT ever letting SR's figure become the charge. It is a pure function; the
caller writes the returned block into the vsr={} evidence and emits the metric.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SrEvidence:
    """The SR observability block attached to the decision log's vsr={} field.
    All fields are EVIDENCE; none is the charge. `divergence_microusd` is
    (sr_reported_cost - ledger_charge); `divergence_ratio` normalizes it so an
    alert threshold is scale-free."""
    origin: str                       # "semantic-router"
    sr_replay_id: Optional[str]
    pool_hash: str
    chosen_model: Optional[str]
    settle_basis: str                 # from settle.SrCharge.basis
    ledger_charge_microusd: int       # charge-of-record (the only truth)
    sr_reported_cost_microusd: Optional[int]
    divergence_microusd: Optional[int]
    divergence_ratio: Optional[float]

    def as_vsr_block(self) -> dict:
        """Serialize for decision_log.build_decision_item(vsr=...). Only non-None
        keys, so the record stays compact."""
        d = {"origin": self.origin, "pool_hash": self.pool_hash,
             "settle_basis": self.settle_basis,
             "ledger_charge_microusd": self.ledger_charge_microusd}
        if self.sr_replay_id is not None:
            d["sr_replay_id"] = self.sr_replay_id
        if self.chosen_model is not None:
            d["suggested_model"] = self.chosen_model
        if self.sr_reported_cost_microusd is not None:
            d["sr_reported_cost_microusd"] = self.sr_reported_cost_microusd
        if self.divergence_microusd is not None:
            d["divergence_microusd"] = self.divergence_microusd
        if self.divergence_ratio is not None:
            # round for a stable, low-cardinality metric dimension.
            d["divergence_ratio"] = round(self.divergence_ratio, 4)
        return d


def build_evidence(
    *,
    sr_replay_id: Optional[str],
    pool_hash: str,
    chosen_model: Optional[str],
    settle_basis: str,
    ledger_charge_microusd: int,
    sr_reported_cost_microusd: Optional[int],
) -> SrEvidence:
    """Compute the divergence between SR's self-reported cost and the ledger charge.

    divergence = sr_reported - ledger. None when SR reported no cost. The ratio is
    divergence / max(ledger, 1) so a divide-by-zero is impossible and the sign is
    preserved (positive = SR thinks it cost more than we charged)."""
    div = None
    ratio = None
    if sr_reported_cost_microusd is not None:
        div = sr_reported_cost_microusd - ledger_charge_microusd
        ratio = div / max(ledger_charge_microusd, 1)
    return SrEvidence(
        origin="semantic-router",
        sr_replay_id=sr_replay_id,
        pool_hash=pool_hash,
        chosen_model=chosen_model,
        settle_basis=settle_basis,
        ledger_charge_microusd=ledger_charge_microusd,
        sr_reported_cost_microusd=sr_reported_cost_microusd,
        divergence_microusd=div,
        divergence_ratio=ratio,
    )


# Alert threshold: |ratio| above this for a settled request is a divergence worth
# paging (stale rate table / SR pricing drift / token mismatch). Evidence only —
# never changes the charge.
_DIVERGENCE_ALERT_RATIO = 0.25


def divergence_is_alarming(ev: SrEvidence) -> bool:
    """True when the SR-vs-ledger gap exceeds the alert threshold. The caller
    emits the alert metric; the charge is unaffected (ledger is truth)."""
    return ev.divergence_ratio is not None and abs(ev.divergence_ratio) >= _DIVERGENCE_ALERT_RATIO
