"""In-process fake vLLM Semantic Router for money-path verification.

Installed via `semantic_router.set_transport_hook`, it lets tests drive every SR
failure mode WITHOUT real hardware (Fable IMPLEMENTATION_PLAN §7): normal
routing, first-byte timeout, chosen-model out of the reserve snapshot, missing
x-vsr-replay-id, missing usage, and an internal double-fire recorded in replay.
This closes the invariants (money fail-closed, final<=reserve, idempotency) that
S1-S7 must hold before a real SR container is stood up.
"""
from __future__ import annotations

from mvp.serving.semantic_router import (
    SrForwardError,
    SrForwardRequest,
    SrForwardResult,
)


def normal(chosen_model="claude-haiku-4-5", inp=100, out=50, replay_id="rpl-1"):
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        return SrForwardResult(chosen_model_raw=chosen_model,
                               usage_input_tokens=inp, usage_output_tokens=out,
                               replay_id=replay_id, raw_cost_microusd=1234)
    return _hook


def timeout():
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        raise SrForwardError("first-byte timeout")
    return _hook


def out_of_snapshot(model="sr-invented-model"):
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        return SrForwardResult(chosen_model_raw=model, usage_input_tokens=100,
                               usage_output_tokens=50, replay_id="rpl-oos")
    return _hook


def no_replay_id(chosen_model="claude-haiku-4-5"):
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        return SrForwardResult(chosen_model_raw=chosen_model, usage_input_tokens=100,
                               usage_output_tokens=50, replay_id=None)
    return _hook


def no_usage(chosen_model="claude-haiku-4-5"):
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        return SrForwardResult(chosen_model_raw=chosen_model, usage_input_tokens=None,
                               usage_output_tokens=None, replay_id="rpl-nousage")
    return _hook


def echoes_span_id(captured: list):
    """Records the forwarded span_id so a test can assert propagation."""
    def _hook(req: SrForwardRequest) -> SrForwardResult:
        captured.append(req.span_id)
        return SrForwardResult(chosen_model_raw="claude-haiku-4-5",
                               usage_input_tokens=10, usage_output_tokens=5,
                               replay_id="rpl-span")
    return _hook
