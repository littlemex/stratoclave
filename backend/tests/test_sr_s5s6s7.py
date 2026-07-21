"""S5 (reservation HMAC), S6 (canary + circuit breaker), S7 (SR-vs-ledger
divergence evidence) unit tests. All pure/deterministic; no real hardware."""
from __future__ import annotations

import pytest

from mvp.sr import canary, hardening, observability
from mvp.sr.reservation import CandidatePool, PoolReservation, PricedCandidate


def _proof(rid="r1", cap=1024, amt=15360):
    pool = CandidatePool("acme", (PricedCandidate("m", 15_000_000, "v1"),), "hp", 1)
    return PoolReservation._mint(reservation_id=rid, pool=pool,
                                 max_tokens_cap=cap, reserve_amount_microusd=amt).consume()


# ------------------------------------------------------------------ S5 HMAC
def test_sign_none_without_key(monkeypatch):
    monkeypatch.delenv("STRATO_SR_RESERVATION_HMAC_KEY", raising=False)
    assert hardening.sign_reservation(_proof(), "acme") is None


def test_sign_verify_roundtrip(monkeypatch):
    monkeypatch.setenv("STRATO_SR_RESERVATION_HMAC_KEY", "secret-money-path-key")
    p = _proof()
    sig = hardening.sign_reservation(p, "acme")
    assert sig and hardening.verify_reservation_sig(p, "acme", sig) is True


def test_verify_rejects_without_key(monkeypatch):
    # a genuine sig, but the verifier has no key -> reject (fail-closed).
    monkeypatch.setenv("STRATO_SR_RESERVATION_HMAC_KEY", "k")
    p = _proof()
    sig = hardening.sign_reservation(p, "acme")
    monkeypatch.delenv("STRATO_SR_RESERVATION_HMAC_KEY", raising=False)
    assert hardening.verify_reservation_sig(p, "acme", sig) is False


def test_verify_rejects_wrong_tenant_or_tampered(monkeypatch):
    monkeypatch.setenv("STRATO_SR_RESERVATION_HMAC_KEY", "k")
    p = _proof()
    sig = hardening.sign_reservation(p, "acme")
    assert hardening.verify_reservation_sig(p, "attacker", sig) is False   # rebinding
    assert hardening.verify_reservation_sig(p, "acme", sig + "00") is False  # tampered
    assert hardening.verify_reservation_sig(p, "acme", None) is False


def test_sig_bound_to_amount_and_cap(monkeypatch):
    # a signature for a small reservation cannot validate a larger one.
    monkeypatch.setenv("STRATO_SR_RESERVATION_HMAC_KEY", "k")
    small = _proof(rid="r", cap=8, amt=120)
    big = _proof(rid="r", cap=100000, amt=1_500_000)
    sig_small = hardening.sign_reservation(small, "acme")
    assert hardening.verify_reservation_sig(big, "acme", sig_small) is False


# ------------------------------------------------------------------ S6 canary
def test_canary_deterministic(monkeypatch):
    a = canary.in_canary("acme", "conv-1", canary_bps=5000)
    b = canary.in_canary("acme", "conv-1", canary_bps=5000)
    assert a == b   # session-sticky: same conversation -> same decision


def test_canary_zero_and_full():
    assert canary.in_canary("t", "c", canary_bps=0) is False
    assert canary.in_canary("t", "c", canary_bps=10000) is True


def test_canary_fraction_is_roughly_bps():
    hits = sum(canary.in_canary("t", f"conv-{i}", canary_bps=1000) for i in range(2000))
    # ~10% of 2000 = ~200; allow a wide band (determinism, not a tight RNG test).
    assert 120 <= hits <= 300


def test_circuit_breaker_trips_and_blocks(monkeypatch):
    canary.reset_for_test()
    assert canary.circuit_open() is False
    canary.trip("out-of-snapshot model")
    assert canary.circuit_open() is True
    canary.reset_for_test()
    assert canary.circuit_open() is False


# ------------------------------------------------------------------ S7 divergence
def test_evidence_divergence_positive():
    ev = observability.build_evidence(
        sr_replay_id="rpl-1", pool_hash="hp", chosen_model="claude-haiku-4-5",
        settle_basis="measured", ledger_charge_microusd=100,
        sr_reported_cost_microusd=125)
    assert ev.divergence_microusd == 25
    assert abs(ev.divergence_ratio - 0.25) < 1e-9
    assert observability.divergence_is_alarming(ev) is True   # 25% hits the threshold


def test_evidence_small_divergence_not_alarming():
    ev = observability.build_evidence(
        sr_replay_id="r", pool_hash="hp", chosen_model="m", settle_basis="measured",
        ledger_charge_microusd=1000, sr_reported_cost_microusd=1010)
    assert observability.divergence_is_alarming(ev) is False


def test_evidence_no_sr_cost_has_no_divergence():
    ev = observability.build_evidence(
        sr_replay_id=None, pool_hash="hp", chosen_model=None,
        settle_basis="reserve-fallback:no-usage", ledger_charge_microusd=500,
        sr_reported_cost_microusd=None)
    assert ev.divergence_microusd is None
    assert observability.divergence_is_alarming(ev) is False


def test_evidence_as_vsr_block_charge_is_ledger():
    ev = observability.build_evidence(
        sr_replay_id="rpl", pool_hash="hp", chosen_model="claude-opus-4-7",
        settle_basis="measured", ledger_charge_microusd=777,
        sr_reported_cost_microusd=800)
    block = ev.as_vsr_block()
    # the charge-of-record in the evidence block is the LEDGER's, never SR's.
    assert block["ledger_charge_microusd"] == 777
    assert block["sr_reported_cost_microusd"] == 800   # SR figure kept as evidence
    assert block["origin"] == "semantic-router"
    assert block["suggested_model"] == "claude-opus-4-7"
