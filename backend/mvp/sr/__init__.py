"""Semantic Router (SR) integration seam.

This package is the home for integrating the **vLLM Semantic Router** (the real
OSS component, github.com/vllm-project/semantic-router) as Stratoclave's routing
brain — "the SR chooses, Stratoclave accounts". It replaces three home-grown
pieces that were built when "VSR" was mis-read as a self-hosted "Value/Session
Router": the self-hosted SAAR router, the self-hosted external advisor, and the
per-provider served_by breadth fan-out.

Migration is staged (see docs). Stage 1 introduces this seam's TYPES only
(`port.py`) with NO wiring — the legacy SAAR / advisor keep running behind
adapters that satisfy this same port, so routing behaviour is unchanged until an
SR adapter is plugged in and its flag flipped in a later stage.

The invariant this seam enforces: **routing is fail-open, money is fail-closed.**
A router (legacy or SR) only ever *supplies a decision*; the atomic reserve→settle
ledger remains the sole charge-of-record and the sole gate on spend.
"""
