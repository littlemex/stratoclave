"""A virtual registry entry may never be the model a charge is recorded against.

WHAT DEFECT THIS CLOSES

`served_by="semantic-router"` entries are VIRTUAL: an entry names a router pool rather
than a concrete model, and it exists to be a candidate chain and a reservation entry
point. The model of record has to be whatever the router actually executed, normalised
from the replay evidence and priced at the snapshot the reservation froze.

That property is the reason a router can live outside this gateway at all. The charge is
derived from what ran, so the router's advice is an input rather than a source of money,
and "the bill did not drift when the routing policy changed" is a checkable claim instead
of a hope. Price the pool entry instead and the bill silently becomes "what the router was
asked for"; nothing downstream would notice, and every drift measurement taken against it
would stop meaning anything.

Until this file existed, that property was a docstring. The registry refuses to LOAD a
malformed `virtual` flag, which is a different guarantee — it says the flag is well-formed,
not that nothing bills against it. The semantic-router seam currently ships dark, so the
gap was unreachable rather than absent, which is the cheapest possible moment to close it:
the adapter that fills the seam is not written yet, and it will now fail here rather than
in a reconciliation report months later.
"""
from __future__ import annotations

import pytest

from mvp import _pipeline
from mvp.models import ModelEntry


class _Repo:
    """Enough of a reservation context to reach the guard. Deliberately explosive: if
    the guard ever stops raising, the settle continues into this and the test fails
    loudly rather than passing because nothing happened."""

    def refund(self, **kwargs):
        raise AssertionError(
            "the settle proceeded past the virtual-entry guard and started moving money")


class _User:
    user_id = "u-virtual"
    org_id = "acme"


def _entry(**over) -> ModelEntry:
    """A registry entry built from the real frozen dataclass, so a field this test
    invents cannot exist and a field the registry adds shows up here as a TypeError
    rather than as a silently-ignored dict key."""
    fields = {
        "provider": "anthropic",
        "bedrock_model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "bedrock_region": "us-east-1",
        "aliases": (),
        "wire_protocol": "messages",
        "pricing_key": "haiku",
    }
    fields.update(over)
    return ModelEntry(**fields)


def _settle(model_id: str):
    return _pipeline.settle_reservation_and_log(
        user=_User(), tenants_repo=_Repo(), reservation=4000,
        actual_input_tokens=10, actual_output_tokens=20, model_id=model_id,
    )


def test_a_virtual_entry_cannot_be_the_model_of_record(monkeypatch):
    """The guarantee. A settle handed a virtual pool entry refuses rather than pricing
    it, because there is no correct amount to charge for a pool."""
    virtual = _entry(bedrock_model_id="sr-pool-cheap", served_by="semantic-router",
                     sr_pool_ref="cheap", virtual=True)
    monkeypatch.setattr(_pipeline_models(), "resolve_model", lambda name: virtual)

    with pytest.raises(ValueError) as caught:
        _settle("sr-pool-cheap")
    assert "virtual" in str(caught.value)
    assert "actually executed" in str(caught.value), (
        "the error has to say what to settle with instead, or the next person to hit it "
        "will reach for the pool entry's rate")


def test_a_concrete_entry_still_settles(monkeypatch):
    """The other direction, or the guard could be `raise` on every path and pass above.
    A real model reaches the money code — which this repo's explosive `_Repo` proves by
    failing differently."""
    concrete = _entry()
    monkeypatch.setattr(_pipeline_models(), "resolve_model", lambda name: concrete)

    with pytest.raises(AssertionError) as caught:
        _settle("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert "past the virtual-entry guard" in str(caught.value), (
        "a concrete model must NOT be refused by the guard; it has to reach the settle")


def test_an_unresolvable_model_is_not_this_guards_error(monkeypatch):
    """Scope. An id the registry cannot resolve is another layer's failure, and turning
    it into a settle refusal here would strand money on a request that was already
    served. The guard answers the virtual question only."""
    def _boom(name):
        raise KeyError(name)

    monkeypatch.setattr(_pipeline_models(), "resolve_model", _boom)
    with pytest.raises(AssertionError) as caught:
        _settle("something-nobody-registered")
    assert "past the virtual-entry guard" in str(caught.value)


def _pipeline_models():
    """The module object `_pipeline` imports `resolve_model` FROM, so the patch lands on
    the reference the guard actually reads rather than on a same-named symbol nobody
    calls."""
    from mvp import models

    return models
