"""The hold is the only way to end a reservation, and it ends exactly once.

Two kinds of check live here, and they cover different failure modes.

The STATIC check is the one that keeps the design from decaying: a route that
calls `refund` / `release_pool` / `settle_reservation_and_log` itself has a money
ending nobody reviewed, and the liability classifier is not consulted on it. That
is exactly how eight of the nine endings came to disagree about what a failed
request costs. Adding a wire format must add no money code, so any call outside
the single `_open_hold` factory fails the build.

The BEHAVIOURAL checks pin the laws the routes rely on: what a settle charges,
when a reservation may be returned, that a stream which produced nothing is not
silently free once enforcement is on, and that no control flow — including a
generator `finally` racing an offloaded write — can produce two money writes.
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path

import pytest
from botocore.exceptions import ParamValidationError, ReadTimeoutError

from mvp import _money
from mvp._money import run_ending
from mvp import provider_outcome as po

BACKEND = Path(__file__).resolve().parents[1]

#: Ending a reservation is exactly these three moves. `refund` is matched on the
#: attribute name so `tenants_repo.refund(...)` is caught however the repo is
#: spelled.
MONEY_CALLS = frozenset({
    "refund",
    "release_pool",
    "_release_pool",
    "settle_reservation_and_log",
    "_settle_reservation_and_log",
})

#: The scan is over EVERY module under `mvp/`, not a list of the ones that have a
#: money path today: a new wire format must be covered by being new, not by
#: someone remembering to add it here. Each exemption names why it moves money for
#: a reason that is not ending an inference reservation.
EXEMPT = {
    "mvp/_money.py": "the hold itself — this is where the moves live",
    "mvp/_pipeline.py": "defines settle_reservation_and_log / release_pool",
    "mvp/billing_authorize.py": (
        "Layer 5 external authorize/capture: a non-LLM charge whose terminal is "
        "the caller's own capture or void, deliberately outside the hold"
    ),
    "mvp/admin_tenants.py": "administrative credit operations, not a request ending",
    "mvp/credit_ops.py": "administrative credit operations, not a request ending",
    "mvp/admin_users.py": "administrative credit operations, not a request ending",
    "mvp/team_lead.py": "administrative credit operations, not a request ending",
    "mvp/billing_read.py": "read-only billing views",
}

#: The one function per module allowed to name the moves: the factory that hands
#: the callables to the hold. Required to be at module level — a local function of
#: the same name inside a route would otherwise be a way through the guard.
FACTORY = "_open_hold"


def _module_files() -> list[str]:
    return sorted(
        str(path.relative_to(BACKEND))
        for path in (BACKEND / "mvp").rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _money_calls_outside_the_factory(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text())
    found: list[tuple[int, str, str]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _visit_scope(self, node) -> None:
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _visit_scope
        visit_AsyncFunctionDef = _visit_scope
        visit_ClassDef = _visit_scope

        def visit_Call(self, node: ast.Call) -> None:
            name = _called_name(node)
            # `self.stack == [FACTORY]` — module-level factory only, so a nested
            # helper that happens to share the name does not inherit the licence.
            if name in MONEY_CALLS and self.stack != [FACTORY]:
                found.append((node.lineno, "::".join(self.stack) or "<module>", name))
            self.generic_visit(node)

    Visitor().visit(tree)
    return found


def test_the_exemption_list_names_only_real_files():
    """An exemption for a file that no longer exists is a hole with a comment."""
    missing = [rel for rel in EXEMPT if not (BACKEND / rel).exists()]
    assert not missing, f"exempted files that do not exist: {missing}"


@pytest.mark.parametrize("relpath", _module_files())
def test_a_route_cannot_end_a_reservation_by_hand(relpath):
    if relpath in EXEMPT:
        pytest.skip(f"exempt: {EXEMPT[relpath]}")
    offenders = _money_calls_outside_the_factory(BACKEND / relpath)
    assert not offenders, (
        f"{relpath} ends a reservation outside its hold:\n"
        + "\n".join(f"  line {ln}: {where} calls {what}" for ln, where, what in offenders)
        + "\n\nRoutes report what they observed; the hold decides. Use "
          "hold.claim_settle(usage) / hold.claim_unobserved(exc=…) / "
          "hold.claim_stream_interrupted(usage, provider_responded=…) / hold.close(…)."
    )


# ---------------------------------------------------------------------------
# the laws the routes rely on
# ---------------------------------------------------------------------------


class _Repo:
    hold_id = "hold-1"

    def __init__(self) -> None:
        self.refunded: list[int] = []

    def refund(self, *, user_id, tenant_id, tokens):
        self.refunded.append(tokens)


class _User:
    user_id = "u"
    org_id = "acme"


def _hold(*, departed: bool = True, **kw):
    """A hold whose provider call has already been handed to the transport.

    `departed=True` is the default because every test in this file models a failure
    that came back FROM a provider — a read timeout, a 5xx, a stream that produced
    nothing — and retention is scoped to requests that actually left. A test for the
    other case (an exception raised before the call, where holding the tenant's budget
    would invent a liability) passes `departed=False`; see
    `test_retention_requires_departure.py` for that axis in full.
    """
    repo = kw.pop("repo", None) or _Repo()
    settled: list[dict] = []
    released: list[bool] = []
    hold = _money.Hold(
        user=_User(), tenants_repo=repo, reservation=4000,
        model_id="model-x",
        settle=lambda **k: settled.append(k),
        release=lambda ctx: released.append(True),
        **kw,
    )
    if departed:
        hold.provider_call_starting()
    return hold, repo, settled, released


def test_a_settle_charges_the_four_token_legs_it_was_given():
    hold, repo, settled, released = _hold()
    assert run_ending(hold.claim_settle(_money.Usage(11, 22, 33, 44))) == po.SETTLED_FINAL
    assert len(settled) == 1
    assert settled[0]["actual_input_tokens"] == 11
    assert settled[0]["actual_output_tokens"] == 22
    assert settled[0]["actual_cache_read_tokens"] == 33
    assert settled[0]["actual_cache_write_tokens"] == 44
    assert repo.refunded == [] and released == []


def test_an_accumulator_is_accepted_wherever_a_usage_is():
    from mvp import _converse_types as t

    acc = t.UsageAccumulator()
    acc.input_tokens, acc.output_tokens = 5, 6
    hold, _repo, settled, _released = _hold()
    run_ending(hold.claim_settle(acc))
    assert (settled[0]["actual_input_tokens"], settled[0]["actual_output_tokens"]) == (5, 6)


def test_a_failure_before_the_wire_returns_the_reservation():
    hold, repo, settled, released = _hold()
    state = run_ending(hold.claim_unobserved(exc=ParamValidationError(report="nope")))
    assert state == po.NOT_SUBMITTED
    assert repo.refunded == [4000] and released == [True]
    assert settled == []


def test_a_read_timeout_keeps_the_reservation_once_enforcement_is_on(monkeypatch):
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, settled, released = _hold()
    state = run_ending(hold.claim_unobserved(exc=ReadTimeoutError(endpoint_url="https://x")))
    assert state == po.SUBMITTED_UNSETTLED
    assert repo.refunded == [], "a call that may have been billed was refunded"
    assert released == [] and settled == []


def test_the_same_read_timeout_still_refunds_with_the_gate_off(monkeypatch):
    """Merging the unification moves no money: with the gate off the behaviour is
    what it was, and the classification is still recorded."""
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "0")
    hold, repo, _settled, released = _hold()
    run_ending(hold.claim_unobserved(exc=ReadTimeoutError(endpoint_url="https://x")))
    assert repo.refunded == [4000] and released == [True]


@pytest.mark.parametrize("status", sorted(po._REJECTION_STATUSES_BY_SHAPE))
def test_an_upstream_refusal_returns_the_reservation(status, monkeypatch):
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, _settled, released = _hold()
    assert run_ending(hold.claim_unobserved(status_code=status)) == po.REJECTED_PRE_INFERENCE
    assert repo.refunded == [4000] and released == [True]


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_a_timeout_or_a_server_failure_does_not(status, monkeypatch):
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, _settled, released = _hold()
    assert run_ending(hold.claim_unobserved(status_code=status)) == po.SUBMITTED_UNSETTLED
    assert repo.refunded == [] and released == []


def test_a_200_we_cannot_parse_is_not_a_free_request(monkeypatch):
    """The model ran; we simply cannot read what it did."""
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, _settled, released = _hold()
    run_ending(hold.claim_unobserved(state=po.SUBMITTED_UNSETTLED))
    assert repo.refunded == [] and released == []


def test_abandon_refuses_to_be_used_for_an_observed_outcome():
    hold, _repo, _settled, _released = _hold()
    with pytest.raises(ValueError):
        run_ending(hold.claim_unobserved(state=po.SETTLED_FINAL))
    assert not hold.claimed, "a rejected call must not consume the ending"


def test_a_partial_stream_is_charged_for_what_arrived():
    hold, repo, settled, _released = _hold()
    hold.claim_stream_interrupted(_money.Usage(10, 3), provider_responded=True).run()
    assert hold.outcome_state == po.SETTLED_FINAL
    assert settled[0]["actual_output_tokens"] == 3
    assert repo.refunded == []


def test_a_stream_that_produced_nothing_is_not_silently_free(monkeypatch):
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, settled, released = _hold()
    hold.claim_stream_interrupted(
        _money.Usage(), provider_responded=False,
        exc=ReadTimeoutError(endpoint_url="https://x"),
    ).run()
    state = hold.outcome_state
    assert state == po.SUBMITTED_UNSETTLED
    assert settled == [] and repo.refunded == [] and released == []


def test_and_with_the_gate_off_it_settles_its_zero_exactly_as_before(monkeypatch):
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "0")
    hold, _repo, settled, _released = _hold()
    hold.claim_stream_interrupted(
        _money.Usage(), provider_responded=False, exc=RuntimeError("cut")
    ).run()
    assert hold.outcome_state == po.SETTLED_FINAL
    assert len(settled) == 1 and settled[0]["actual_output_tokens"] == 0


# ---------------------------------------------------------------------------
# exactly once, whatever the control flow does
# ---------------------------------------------------------------------------


def test_the_second_ending_is_a_no_op_whichever_order_it_arrives_in():
    hold, repo, settled, released = _hold()
    assert run_ending(hold.claim_settle(_money.Usage(1, 1))) == po.SETTLED_FINAL
    assert run_ending(hold.claim_settle(_money.Usage(9, 9))) is None
    run_ending(hold.claim_unobserved(exc=ParamValidationError(report="nope")))
    assert len(settled) == 1 and repo.refunded == [] and released == []

    hold2, repo2, settled2, released2 = _hold()
    run_ending(hold2.claim_unobserved(exc=ParamValidationError(report="nope")))
    assert run_ending(hold2.claim_settle(_money.Usage(1, 1))) is None
    assert settled2 == [] and repo2.refunded == [4000]


def test_a_hold_under_concurrent_endings_writes_exactly_once():
    """The `finally` of a closing generator can race the offloaded write, which is
    how the double-settle (pool over-admission plus a double bill) happened."""
    hold, _repo, settled, _released = _hold()
    start = threading.Barrier(8)

    def race():
        start.wait()
        run_ending(hold.claim_settle(_money.Usage(2, 2)))

    threads = [threading.Thread(target=race) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(settled) == 1


def test_the_observability_hook_only_runs_for_the_winner_and_cannot_break_a_settle():
    notes: list[str] = []

    def hook(status, observation):
        notes.append(status)
        raise RuntimeError("observability must never affect the request")

    hold, _repo, settled, _released = _hold(on_finalized=hook)
    run_ending(hold.claim_settle(_money.Usage(1, 1)))
    run_ending(hold.claim_settle(_money.Usage(1, 1)))
    assert notes == ["completed"], "the hook ran for a loser, or not at all"
    assert len(settled) == 1, "a raising hook cost the request its settle"


def test_the_correlation_marker_carries_the_hold_and_the_tenant():
    hold, _repo, _settled, _released = _hold()
    assert hold.request_metadata() == {"sc_attempt_id": "hold-1", "sc_tenant": "acme"}


# ---------------------------------------------------------------------------
# the claim is separable from the write, and that ORDER is what protects the
# KIND of ending — a latch alone only protects the count
# ---------------------------------------------------------------------------


def test_a_claimed_ending_cannot_be_replaced_by_a_later_one():
    """Claim, then let a competing ending try. The write has not run yet, and the
    hold must still refuse the second ending — otherwise a cancellation between
    claim and write turns a classified abandon into a zero settle."""
    hold, repo, settled, released = _hold()
    commit = hold.claim_unobserved(exc=ParamValidationError(report="nope"))
    assert commit is not None
    # Nothing written yet, but the ending is owned.
    assert settled == [] and repo.refunded == []
    assert hold.claim_stream_interrupted(
        _money.Usage(9, 9), provider_responded=True, status="client_disconnect"
    ) is None
    assert hold.claim_settle(_money.Usage(9, 9)) is None
    commit.run()
    assert repo.refunded == [4000] and released == [True] and settled == []


def test_a_lost_claim_returns_no_write_at_all():
    hold, _repo, settled, _released = _hold()
    first = hold.claim_settle(_money.Usage(1, 1))
    second = hold.claim_settle(_money.Usage(2, 2))
    assert first is not None and second is None
    first.run()
    assert len(settled) == 1


def test_a_hook_cannot_change_what_is_charged():
    """The hook receives the live accumulator. The charge is snapshotted first."""
    class _Mutable:
        input_tokens = 10
        output_tokens = 20
        cache_read_tokens = 0
        cache_write_tokens = 0

    observation = _Mutable()

    def hook(status, obs):
        obs.output_tokens = 0

    hold, _repo, settled, _released = _hold(on_finalized=hook)
    run_ending(hold.claim_settle(observation))
    assert settled[0]["actual_output_tokens"] == 20


def test_a_stream_the_provider_answered_without_usage_is_not_free(monkeypatch):
    """On Converse the usage block is the LAST event, so a cut stream leaves the
    counters at zero while the request demonstrably reached the model service.
    Reading that as a zero settle is the free-tokens defect in stream clothing."""
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, settled, released = _hold()
    hold.claim_stream_interrupted(
        _money.Usage(), provider_responded=True, exc=RuntimeError("cut")
    ).run()
    state = hold.outcome_state
    assert state == po.SUBMITTED_UNSETTLED
    assert settled == [] and repo.refunded == [] and released == []


def test_two_readings_of_one_attempt_are_refused():
    """`state` and `exc` together would resolve by an implicit precedence, and the
    cheap reading winning is how a billed call gets refunded."""
    hold, _repo, _settled, _released = _hold()
    with pytest.raises(ValueError):
        hold.claim_unobserved(exc=ReadTimeoutError(endpoint_url="https://x"),
                              state=po.REJECTED_PRE_INFERENCE)
    assert not hold.claimed


def test_a_claimed_ending_is_written_even_if_its_dispatch_is_interrupted():
    """The endings claim before the frame that announces them goes out, so a
    consumer can close between the claim and the write. The claim is not
    returnable, so the write cannot be optional."""
    hold, _repo, settled, _released = _hold()
    ending = hold.claim_settle(_money.Usage(4, 5))
    assert ending is not None and not ending.started
    assert hold.dispatch_pending() is True, "a claimed ending was left unwritten"
    assert len(settled) == 1 and settled[0]["actual_output_tokens"] == 5
    # And it is not written twice by a second sweep.
    assert hold.dispatch_pending() is False
    assert len(settled) == 1


def test_a_dispatched_ending_is_not_swept_again():
    hold, _repo, settled, _released = _hold()
    ending = hold.claim_settle(_money.Usage(1, 1))
    assert ending is not None
    ending.run()
    assert hold.dispatch_pending() is False
    assert len(settled) == 1


def test_a_disconnect_before_the_provider_answered_is_not_a_free_pass(monkeypatch):
    """`disconnected` must not be a way around the policy: with nothing observed
    and no answer from the provider it resolves exactly as a cut stream does."""
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")
    hold, repo, settled, released = _hold()
    hold.close(_money.Usage(), sent=True, provider_responded=False)
    assert hold.outcome_state == po.SUBMITTED_UNSETTLED
    assert settled == [] and repo.refunded == [] and released == []


def test_a_request_that_never_reached_the_provider_is_returned_not_settled():
    hold, repo, settled, released = _hold()
    ending = hold.claim_not_submitted()
    assert ending is not None
    ending.run()
    assert hold.outcome_state == po.NOT_SUBMITTED
    assert settled == [], "a request that was never sent recorded usage"
    assert repo.refunded == [4000] and released == [True]


def test_an_unknown_state_is_refused_before_it_can_consume_the_ending():
    hold, _repo, _settled, _released = _hold()
    with pytest.raises(ValueError):
        hold.claim_unobserved(state="probably_fine")
    assert not hold.claimed, "the hold lost its ending to a typo"


def test_a_malformed_observation_is_refused_before_the_claim():
    class _Bad:
        input_tokens = "twelve"
        output_tokens = 0
        cache_read_tokens = 0
        cache_write_tokens = 0

    hold, _repo, _settled, _released = _hold()
    with pytest.raises(ValueError):
        hold.claim_settle(_Bad())
    assert not hold.claimed, "the hold lost its ending to an unreadable usage block"


def test_a_hook_that_raises_a_base_exception_does_not_cost_the_write():
    def hook(status, obs):
        raise KeyboardInterrupt("not an Exception subclass")

    hold, _repo, settled, _released = _hold(on_finalized=hook)
    run_ending(hold.claim_settle(_money.Usage(2, 2)))
    assert len(settled) == 1
