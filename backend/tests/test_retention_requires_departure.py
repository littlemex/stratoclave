"""Retention is for money that may have been spent, not for our own crashes.

WHAT DEFECT THIS CLOSES

`classify_exception` ends in `SUBMITTED_UNSETTLED` rather than in a guess, which is
the right answer for an exception that came back from the provider: the call left, the
model may have run to completion, and what it cost is unobservable. But the catch-all
caught more than that. Any exception the classifier did not recognise landed in the
same state, including one raised by our OWN code before the request reached the
transport — a bug in body assembly, a helper raising `RuntimeError`. While the
retention flag shipped off this was harmless, because both branches refunded. With the
flag on by default it stopped being harmless: a crash inside this gateway retained the
tenant's reservation, and it stayed retained until an operator went and released it by
hand. A gateway that eats a customer's budget when its own code breaks is worse than
one that refunds a call it should have charged.

The fix is not a longer exception taxonomy. It is one fact recorded where it is known:
`Hold.provider_call_starting()`, called at each provider call site immediately before
the client is invoked. An unobserved ending with that fact is retained; without it the
request never left, nothing can have been billed, and the reservation is returned.

The second test here is the one that matters in a year. A new provider route added
later will have its own invoke site, and nothing about writing one suggests you must
announce it. So the check is structural: every module that invokes a provider client
must contain the announcement, found by parsing the source rather than by remembering.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from mvp import _money, provider_outcome as _outcome


ROOT = pathlib.Path(__file__).resolve().parents[1]


class _FakeRepo:
    """The minimum a `Hold` reads off its reservation context."""

    def __init__(self) -> None:
        self.hold_id = "h-departure"
        self.hold_sk = "sk-departure"
        self.refunds: list[int] = []

    def refund(self, *args, **kwargs):
        """Returning the token reservation. Recorded rather than ignored so a test
        cannot pass because nothing happened at all."""
        self.refunds.append(args[0] if args else kwargs.get("tokens", 0))
        return None


def _hold(**kw):
    calls: dict[str, int] = {"released": 0, "kept": 0, "marked": 0}

    def _release(_ctx):
        calls["released"] += 1

    def _mark(_state):
        calls["marked"] += 1
        return True

    hold = _money.Hold(
        user=type("U", (), {"org_id": "acme", "user_id": "u-1"})(),
        tenants_repo=_FakeRepo(),
        reservation=4000,
        model_id="us.anthropic.claude-opus-4-7",
        settle=lambda **_kw: None,
        release=_release,
        mark_departed=_mark,
        route="test",
        **kw,
    )
    return hold, calls


def test_an_unobserved_ending_that_never_reached_the_transport_is_returned(monkeypatch):
    """The defect, as a behaviour. The flag is ON — its shipped default — and the
    outcome is the expensive one the classifier falls back to, so the only thing
    standing between our own crash and the tenant's budget is the departure fact."""
    monkeypatch.setenv(_outcome.UNOBSERVED_HOLD_ENV, "1")
    hold, calls = _hold()
    # Deliberately NOT calling provider_call_starting(): this stands for an exception
    # raised while assembling the request, which classify_exception cannot tell apart
    # from a read timeout.
    ending = hold.claim_unobserved(exc=RuntimeError("a bug in our own code"))
    assert ending is not None
    ending.run()

    assert calls["released"] == 1, (
        "a request that never reached the transport cannot have been billed, so its "
        "reservation must come back rather than be held against the tenant")
    assert calls["marked"] == 0, (
        "nothing departed, so writing a departure marker would record a liability "
        "that does not exist")


def test_the_same_ending_after_the_call_started_is_retained(monkeypatch):
    """The other half, or the test above would pass with retention deleted entirely.
    Same flag, same exception, same code path — only the departure fact differs."""
    monkeypatch.setenv(_outcome.UNOBSERVED_HOLD_ENV, "1")
    hold, calls = _hold()
    hold.provider_call_starting()
    ending = hold.claim_unobserved(exc=RuntimeError("the stream broke mid-flight"))
    assert ending is not None
    ending.run()

    assert calls["released"] == 0, (
        "the request left, so the provider may have billed it; handing the budget "
        "back records that the call was free")
    assert calls["marked"] == 1, (
        "the reaper meets this hold with no memory of this moment and needs the "
        "departure written down")


@pytest.mark.parametrize("state", sorted(_outcome.STATES))
def test_a_provably_cheap_outcome_is_returned_either_way(monkeypatch, state):
    """The departure fact must not become a second, competing rule for the states
    the classifier already decides. An outcome that refunds immediately does so
    whether or not the call started; anything else is a new way to strand money."""
    if not _outcome.refunds_immediately(state):
        pytest.skip("not a refunds-immediately state")
    monkeypatch.setenv(_outcome.UNOBSERVED_HOLD_ENV, "1")
    for started in (False, True):
        hold, calls = _hold()
        if started:
            hold.provider_call_starting()
        ending = hold.claim_unobserved(state=state)
        assert ending is not None
        ending.run()
        assert calls["released"] == 1, (
            f"{state} refunds immediately, so started={started} must not change it")


def _modules_that_claim_an_unobserved_ending() -> dict[str, list[int]]:
    """Modules under `mvp/` that end a reservation as an unobserved outcome, with the
    line numbers, found by parsing rather than by listing.

    This is deliberately keyed on `claim_unobserved` rather than on the shape of the
    provider call. A first version of this test swept for the call shapes instead —
    `converse`, `converse_stream` — and missed a fourth, `client.stream("POST", ...)`
    on the OpenAI-compatible streaming path, which therefore shipped without the
    announcement. Enumerating call shapes means maintaining a list of the ways one can
    reach a provider, and a fifth transport arrives without asking. Claiming an
    unobserved ending is the thing that cannot be done accidentally, and it is exactly
    the operation whose correctness depends on the departure fact: a module that
    decides "the cost of this is unobservable" must also be a module that knows
    whether anything left.
    """
    found: dict[str, list[int]] = {}
    for path in sorted((ROOT / "mvp").rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "_money.py":
            continue  # _money.py DEFINES the claim; it does not decide a route's
        tree = ast.parse(path.read_text())
        lines = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("claim_unobserved", "claim_stream_interrupted")
        ]
        if lines:
            found[str(path.relative_to(ROOT))] = lines
    return found


def test_every_module_that_ends_an_unobserved_outcome_announces_the_departure():
    """The structural half. A module that classifies a failure as unobserved without
    ever announcing the hand-off cannot retain: the reservation is refunded on a call
    that may well have been billed. That is the retention silently not firing, which is
    worse than it being off, because the operator believes it is on. The reverse error —
    announcing where nothing departed — is caught by the behavioural tests above."""
    modules = _modules_that_claim_an_unobserved_ending()
    assert modules, (
        "the unobserved-ending sweep found nothing, so this test would accept any "
        "wiring at all")

    silent = sorted(
        rel for rel in modules
        if "provider_call_starting" not in (ROOT / rel).read_text()
    )
    assert not silent, (
        "these modules end a reservation as an unobserved outcome without announcing "
        f"that anything reached the transport, so retention cannot fire there: {silent}"
    )
