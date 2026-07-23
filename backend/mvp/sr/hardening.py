"""S5 hardening: the reservation-signature layer (Fable IMPLEMENTATION_PLAN §3).

Three layers enforce "no SR forward without a reserve". S1-S4 built the TYPE
layer (ConsumedProof required). Network (mTLS ingress, SR not public) and keys
(backend_refs provider keys live only on SR) are IaC/deploy concerns. This module
adds the third code-visible layer: a per-request HMAC over the reservation
identity, so even if the type + network layers were both bypassed, SR rejects a
forward that carries no valid reservation signature (401).

The signing key is money-path only: it is read from STRATO_SR_RESERVATION_HMAC_KEY
which is mounted solely on the money-path task. Nothing else can mint a valid
`x-strato-reservation-sig`, so a forward without a genuine reservation cannot be
forged. Verification here is the SAME computation, used by tests and (later) by an
SR-side sidecar / the fake harness.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from .reservation import ConsumedProof

_SIG_HEADER = "x-strato-reservation-sig"


def _key() -> Optional[bytes]:
    """The HMAC key, money-path only. Absent ⇒ signing disabled (returns None);
    callers treat a None key as 'cannot sign' — fail-closed at the boundary that
    needs a signature, never a silent unsigned forward."""
    raw = os.getenv("STRATO_SR_RESERVATION_HMAC_KEY", "").strip()
    return raw.encode("utf-8") if raw else None


def _canonical(proof: ConsumedProof, tenant_id: str) -> bytes:
    # bind the signature to the exact reservation identity + amount + pool + cap,
    # so a signature cannot be replayed onto a different reservation or a larger
    # cap/amount.
    #
    # P3: length-prefixed framing, NOT "|".join. reservation_id / tenant_id can
    # contain arbitrary characters; a plain delimiter lets ("a|b","c") and
    # ("a","b|c") collide onto the same canonical bytes (a field-boundary slide).
    # Encoding each field as len(bytes)\x00<bytes> makes the framing injective, so
    # no two distinct field tuples share a signature.
    #
    # SCOPE: this binds reservation IDENTITY (id, tenant, cap, amount, pool_hash),
    # NOT the request body or span_id. Swapping the body within the same
    # reservation is signature-valid; the idempotency key + single-consume proof
    # are what prevent re-use, so the residual risk is low. If body binding is ever
    # required, add a body hash as a further length-prefixed field here.
    parts = [
        proof.reservation_id,
        tenant_id,
        str(proof.max_tokens_cap),
        str(proof.reserve_amount_microusd),
        proof.pool.pool_hash,
    ]
    buf = bytearray()
    for p in parts:
        b = p.encode("utf-8")
        buf += f"{len(b)}\x00".encode("utf-8")
        buf += b
    return bytes(buf)


def sign_reservation(proof: ConsumedProof, tenant_id: str) -> Optional[str]:
    """Return the hex HMAC for a reservation, or None if no key is configured.
    Only the money-path task has the key, so only it can produce a valid sig."""
    key = _key()
    if key is None:
        return None
    return hmac.new(key, _canonical(proof, tenant_id), hashlib.sha256).hexdigest()


def verify_reservation_sig(proof: ConsumedProof, tenant_id: str, sig: Optional[str]) -> bool:
    """Constant-time verify a presented signature against the reservation. False
    when the key is unset (cannot verify ⇒ reject) or the sig is missing/mismatched.
    fail-closed: an unverifiable forward is refused, not waved through."""
    key = _key()
    if key is None or not sig:
        return False
    expected = hmac.new(key, _canonical(proof, tenant_id), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def sig_header_name() -> str:
    return _SIG_HEADER
