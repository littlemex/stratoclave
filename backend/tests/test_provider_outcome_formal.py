"""Formal and property coverage for the outcome/liability layer.

`docs/design/charge-loss.md`. `tests/test_provider_outcome.py` covers each branch by
example; this file covers what examples cannot. It is the second draft: two
independent reviews took the first one apart, and the findings are recorded here
because each one is a trap worth not falling into twice.

  * **The Z3 proof is over the shipped function.** The first draft re-implemented
    `_pool_settle_items`' arithmetic in the test file and proved that. Production
    could have swapped two bindings while every proof stayed green. The arithmetic is
    now `mvp._pipeline.pool_deltas`, imported here; z3 `Int` expressions flow through
    it unchanged, so the theorem is about the code.
  * **The properties proved are ones that can fail.** Invariant preservation
    (`headroom == limit - reserved - settled`) turned out to be an algebraic
    identity for this move shape — true with every constraint deleted, which makes it
    worthless as a check and makes the carefully-written domain restrictions inert.
    What a pool move can actually violate is a counter going negative, and what a
    SEQUENCE of moves can violate is at-most-once. Those are proved instead.
  * **Oracles are independent of the thing under test.** The first draft asserted
    `refunds_immediately(s) ⟹ liability_for(s) == NONE`, which is the *definition* of
    `refunds_immediately` — all four rows could have become expensive and it would
    have passed. The expected assignment is now pinned literally, and so are the
    rejection-code sets, which were previously their own oracle.
  * **Non-vacuity means the antecedents are satisfiable**, not that Z3 can refute a
    different, deliberately-wrong claim. That check tested Z3.

On technique: the state domain has four elements, so exhaustive enumeration is a
complete proof over it and Z3 would add only a dependency. The solver earns its place
on the money, which is unbounded, and on sequences, which are not enumerable.
"""
from __future__ import annotations

import pytest
import z3
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ParamValidationError,
    ReadTimeoutError,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from mvp import provider_outcome as po
from mvp._pipeline import _reaped_hold_facts, pool_deltas

Z3_TIMEOUT_MS = 30_000
z3.set_param("smt.random_seed", 0)


def _solver() -> z3.Solver:
    s = z3.Solver()
    s.set("timeout", Z3_TIMEOUT_MS)
    return s


def _prove(solver: z3.Solver, negated_property) -> None:
    """Prove by refutation, with the antecedents checked first.

    Non-vacuity is a property of the CONSTRAINTS: if they are contradictory then
    `constraints ∧ ¬P` is unsat for a degenerate reason and nothing was proved. So
    the constraints are asserted satisfiable before the negation is added.
    """
    assert solver.check() == z3.sat, "constraints are contradictory — proof is vacuous"
    solver.push()
    solver.add(negated_property)
    result = solver.check()
    model = solver.model() if result == z3.sat else None
    solver.pop()
    assert result == z3.unsat, f"counterexample: {model}"


# ---------------------------------------------------------------------------
# The policy table, pinned against an independent oracle
# ---------------------------------------------------------------------------

#: What each state is SUPPOSED to cost, written out rather than derived from the
#: table. `refunds_immediately` is defined as "liability is none", so asserting the
#: two agree tests nothing; this is the assignment a reader should have to change on
#: purpose, and a diff on this literal is the review signal.
_EXPECTED_LIABILITY = {
    po.NOT_SUBMITTED: po.LIABILITY_NONE,
    po.REJECTED_PRE_INFERENCE: po.LIABILITY_NONE,
    po.SUBMITTED_UNSETTLED: po.LIABILITY_FULL_CEILING,
    po.SETTLED_FINAL: po.LIABILITY_OBSERVED,
}


def test_each_state_costs_what_the_contract_says_it_costs():
    assert set(_EXPECTED_LIABILITY) == set(po.STATES)
    for state, expected in _EXPECTED_LIABILITY.items():
        assert po.liability_for(state) == expected, state


def test_refunding_is_exactly_the_zero_liability_states():
    """Which states hand money back, pinned to the literal above.

    Stated over the expected assignment rather than over `liability_for`, so a
    change to the table shows up here instead of being silently agreed with.
    """
    refunding = {s for s in po.STATES if po.refunds_immediately(s)}
    assert refunding == {po.NOT_SUBMITTED, po.REJECTED_PRE_INFERENCE}


@pytest.mark.parametrize("bogus", ["", "SUBMITTED", "submitted_unsettled ", "None",
                                   "settled", "NOT_SUBMITTED", "0"])
def test_a_state_that_does_not_exist_never_refunds(bogus):
    """Fail-expensive on a programming error. Case, whitespace and near-miss
    spellings are each a plausible typo, and the safe answer to all of them is to
    keep the money."""
    assert not po.refunds_immediately(bogus)
    assert po.liability_for(bogus) == po.LIABILITY_FULL_CEILING


def test_the_policy_table_covers_exactly_the_declared_states():
    assert set(po.LIABILITY_POLICY) == set(po.STATES)


def test_every_zero_from_provider_behaviour_names_its_accepted_risk():
    """A zero asserting something about the provider is a bet and must say so; a
    zero asserting something about our own transport is not."""
    for state, row in po.LIABILITY_POLICY.items():
        if row["liability"] != po.LIABILITY_NONE or state == po.NOT_SUBMITTED:
            continue
        assert row["accepted_risk"], state


# ---------------------------------------------------------------------------
# The rejection-code sets, pinned literally
# ---------------------------------------------------------------------------
#
# The first draft iterated `po._REJECTION_CODES` and confirmed the classifier agreed
# with it — circular. Adding `ThrottlingException` to the MEASURED set would have
# been a data defect that no test could see, and it would have claimed a counter had
# been put behind a code that never was.

_EXPECTED_MEASURED = {"ValidationException"}
_EXPECTED_BY_SHAPE = {
    "AccessDeniedException", "ResourceNotFoundException", "ThrottlingException",
    "ServiceQuotaExceededException", "UnrecognizedClientException",
    "IncompleteSignature", "MissingAuthenticationToken",
}


def test_only_measured_codes_are_claimed_as_measured():
    assert po._REJECTION_CODES_MEASURED == _EXPECTED_MEASURED
    assert po._REJECTION_CODES_BY_SHAPE == _EXPECTED_BY_SHAPE
    assert not (po._REJECTION_CODES_MEASURED & po._REJECTION_CODES_BY_SHAPE)


@pytest.mark.parametrize("code", sorted(_EXPECTED_MEASURED | _EXPECTED_BY_SHAPE))
def test_a_rejection_code_classifies_as_a_rejection(code):
    exc = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": 400}},
        "Converse",
    )
    assert po.classify_exception(exc) == po.REJECTED_PRE_INFERENCE


# ---------------------------------------------------------------------------
# The classifier is total, and lands in the expensive state when unsure
# ---------------------------------------------------------------------------

@given(st.text(max_size=40))
@settings(max_examples=200, deadline=None)
def test_an_arbitrary_exception_message_never_makes_a_failure_look_free(msg):
    """Classification must not be steerable by an error string: an upstream echoing
    attacker-controlled text must not talk the gateway into refunding a call that may
    have been billed."""
    class Arbitrary(Exception):
        pass

    assert po.classify_exception(Arbitrary(msg)) == po.SUBMITTED_UNSETTLED


_UNKNOWN_CODES = st.one_of(
    st.sampled_from(["", " ", "ThrottlingException ", "validationexception",
                     "SomeNewBedrockRefusal", "InternalServerException",
                     "ModelErrorException", "ServiceUnavailableException"]),
    st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
            min_size=1, max_size=24),
)


@given(code=_UNKNOWN_CODES, status=st.sampled_from([400, 403, 424, 429, 500, 503]))
@settings(max_examples=300, deadline=None)
def test_a_code_nobody_measured_stays_expensive(code, status):
    """Including padded and mis-cased spellings of codes that ARE listed.

    The first draft used `assume(code.strip() == code)`, which discarded exactly the
    near-miss case worth asserting: `"ThrottlingException "` must not inherit the
    zero that `"ThrottlingException"` has.
    """
    exc = ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "Converse",
    )
    state = po.classify_exception(exc)
    assert state in po.STATES
    if code not in po._REJECTION_CODES:
        assert state == po.SUBMITTED_UNSETTLED, (code, status)


@given(response=st.one_of(
    st.just({}),
    st.just({"Error": {}}),
    st.just({"Error": {"Code": None}}),
    st.just({"ResponseMetadata": {}}),
    st.just({"Error": {"Code": "ValidationException"}}),  # no status at all
))
@settings(max_examples=50, deadline=None)
def test_a_malformed_error_response_does_not_crash_or_go_free(response):
    """A provider or proxy can hand back a shape the SDK did not promise. The
    classifier must survive it, and must not read a missing field as good news."""
    exc = ClientError(response, "Converse")
    state = po.classify_exception(exc)
    assert state in po.STATES
    if response.get("Error", {}).get("Code") not in po._REJECTION_CODES:
        assert state == po.SUBMITTED_UNSETTLED


def test_only_pre_wire_failures_are_free():
    """The line the contract draws: did the request reach the model."""
    never_left = [
        ParamValidationError(report="bad"),
        ConnectTimeoutError(endpoint_url="https://x"),
        EndpointConnectionError(endpoint_url="https://x"),
    ]
    may_have_run = [
        ReadTimeoutError(endpoint_url="https://x"),
        ConnectionClosedError(endpoint_url="https://x"),
    ]
    for exc in never_left:
        assert po.refunds_immediately(po.classify_exception(exc)), type(exc).__name__
    for exc in may_have_run:
        assert not po.refunds_immediately(po.classify_exception(exc)), type(exc).__name__


def test_the_switch_cannot_make_an_unsettled_attempt_look_free(monkeypatch):
    """Enforcement is opt-in and must not touch CLASSIFICATION — otherwise turning
    it off would rewrite the ledger's history rather than relax behaviour."""
    exc = ReadTimeoutError(endpoint_url="https://x")
    for value in ("", "0", "1", "true", "off", "garbage"):
        monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, value)
        assert po.classify_exception(exc) == po.SUBMITTED_UNSETTLED
        assert po.liability_for(po.SUBMITTED_UNSETTLED) == po.LIABILITY_FULL_CEILING


# ---------------------------------------------------------------------------
# The pool arithmetic — over the SHIPPED function, on properties that can fail
# ---------------------------------------------------------------------------

def test_z3_a_reclaim_cannot_drive_the_reserved_counter_negative():
    """This is a property a move CAN violate, unlike invariant preservation.

    A negative `pool_reserved` is how a double-credit shows up in the row, and it is
    what `set_manual_limit`'s repair would later bake into a wrong headroom. The proof
    needs `amount <= reserved` — delete that constraint and Z3 finds the
    counterexample, which is what makes the constraint load-bearing rather than
    decorative.
    """
    reserved, amount = z3.Ints("reserved amount")
    d_res, _d_set, _d_head = pool_deltas(amount, 0)
    s = _solver()
    s.add(reserved >= 0, amount >= 0, amount <= reserved)
    _prove(s, reserved + d_res < 0)


def test_z3_without_the_outstanding_bound_the_counter_can_go_negative():
    """The paired direction: the constraint above is doing work.

    Not a test of Z3 — a test that the property is conditional, which is exactly what
    the first draft's invariant-preservation proof was not.
    """
    reserved, amount = z3.Ints("reserved amount")
    d_res, _, _ = pool_deltas(amount, 0)
    s = _solver()
    s.add(reserved >= 0, amount >= 0)  # the bound deliberately omitted
    s.add(reserved + d_res < 0)
    assert s.check() == z3.sat


def test_z3_a_settle_cannot_book_more_spend_than_it_reserved_plus_the_overrun():
    """`settled` must move by exactly the observed amount, never by the reservation.

    The mutation this catches is binding `:actual` to the reservation — the shape that
    would charge every request its ceiling and look plausible in a diff.
    """
    reserved, actual = z3.Ints("reserved actual")
    _d_res, d_set, _ = pool_deltas(reserved, actual)
    s = _solver()
    s.add(reserved >= 0, actual >= 0)
    _prove(s, d_set != actual)


def test_z3_headroom_moves_by_the_difference_and_by_nothing_else():
    """Pins the third binding against the other two.

    Invariant preservation alone cannot catch a swap of `:dr` and `:dh`, because the
    invariant is an identity for this shape; relating the deltas to each other can.
    """
    reserved, actual = z3.Ints("reserved actual")
    d_res, d_set, d_head = pool_deltas(reserved, actual)
    s = _solver()
    s.add(reserved >= 0, actual >= 0)
    _prove(s, d_head != -(d_res) - d_set)


def test_z3_two_reclaims_of_the_same_hold_double_credit_the_headroom():
    """The sequence property single-move proofs cannot see.

    Each reclaim preserves `headroom == limit - reserved - settled` on its own, and
    applying it twice still preserves it — while crediting the amount back twice.
    That is why at-most-once is enforced by a condition on the hold's Delete
    (`attribute_exists(sk)`) and by the terminal event's `attribute_not_exists`,
    NOT by the arithmetic. This test exists to record that the arithmetic does not
    protect against it, so nobody removes those conditions on the strength of a
    formal proof that never covered them.
    """
    limit, reserved, settled, amount = z3.Ints("limit reserved settled amount")
    headroom = limit - reserved - settled
    d_res, d_set, d_head = pool_deltas(amount, 0)
    once = headroom + d_head
    twice = once + d_head
    s = _solver()
    s.add(limit >= 0, reserved >= 0, settled >= 0, amount > 0, amount <= reserved)
    assert s.check() == z3.sat
    s.add(twice == once)  # a second reclaim being a no-op would be the safe world
    assert s.check() == z3.unsat, "a second reclaim IS a second credit — as expected"


@given(reserved=st.integers(min_value=0, max_value=10**12),
       actual=st.integers(min_value=0, max_value=10**12))
@settings(max_examples=400, deadline=None)
def test_the_row_invariant_survives_the_shipped_deltas(reserved, actual):
    """Concrete differential over the same function the SMT tests use.

    Kept even though the property is an identity: it is cheap, and it is what would
    fail first if `pool_deltas` were changed to return something that is not a
    conservative move.
    """
    limit, res0, set0 = 10**13, reserved, actual
    head0 = limit - res0 - set0
    d_res, d_set, d_head = pool_deltas(reserved, actual)
    assert (head0 + d_head) == limit - (res0 + d_res) - (set0 + d_set)


def test_the_settle_fragment_binds_the_deltas_it_computes():
    """Closes the last gap between the proofs and the wire.

    `pool_deltas` being right does not make `_pool_settle_items` right: it could bind
    the values to the wrong attribute names. Read the fragment it actually builds.
    """
    from mvp._pipeline import _pool_settle_items

    item = _pool_settle_items(
        table_name="t", tenant_id="acme", period="2026-08",
        reserved_microusd=500, actual_microusd=120,
    )["Update"]
    d_res, d_set, d_head = pool_deltas(500, 120)
    values = item["ExpressionAttributeValues"]
    assert values[":dr"] == {"N": str(d_res)}
    assert values[":actual"] == {"N": str(d_set)}
    assert values[":dh"] == {"N": str(d_head)}
    expr = item["UpdateExpression"]
    assert "pool_reserved_microusd :dr" in expr
    assert "pool_settled_microusd :actual" in expr
    assert "pool_headroom_microusd :dh" in expr


# ---------------------------------------------------------------------------
# The reaped-hold projection
# ---------------------------------------------------------------------------

_HOLD_KEYS = ("source", "created_at", "provider_invoked_at",
              "amount_microusd", "expires_at", "hold_id", "status", "period")
_HOLD_VALUES = st.one_of(
    st.text(max_size=20),
    st.integers(min_value=-5, max_value=10**9),
    st.decimals(min_value=0, max_value=10**9, allow_nan=False, allow_infinity=False,
                places=0),
    st.none(),
    st.booleans(),
    st.lists(st.text(max_size=3), max_size=2),
)


@given(hold=st.dictionaries(
    keys=st.one_of(st.sampled_from(_HOLD_KEYS), st.text(max_size=8)),
    values=_HOLD_VALUES, max_size=10))
@settings(max_examples=400, deadline=None)
def test_the_reclaim_record_invents_nothing_and_survives_any_row(hold):
    """Every fact copied out must be present in the row, and no row shape may raise.

    Values include `Decimal`, `None`, booleans and lists because that is what a
    DynamoDB item deserialises to — the first draft sampled only text and ints, which
    is not the domain this function is called on. The reclaim deletes the row, so a
    record containing anything the row did not is fabricated evidence, which is worse
    than no evidence: it reads as measurement.
    """
    facts = _reaped_hold_facts(hold)
    for key, value in facts.items():
        assert key in hold, key
        if isinstance(value, int):
            assert int(hold[key]) == value
        else:
            assert str(hold[key]) == value


def test_a_full_row_projects_to_exactly_the_expected_record():
    """The other direction: not just "nothing extra" but "nothing dropped".

    An empty dict satisfies the projection property above, so on its own that
    property would accept a function that preserves nothing.
    """
    from decimal import Decimal

    facts = _reaped_hold_facts({
        "source": "inline",
        "created_at": "2026-08-29T00:00:00Z",
        "provider_invoked_at": "2026-08-29T00:00:01Z",
        "amount_microusd": Decimal("250000"),
        "expires_at": Decimal("1787958108"),
        "hold_id": "h-1",          # deliberately not copied
        "status": "ACTIVE",        # deliberately not copied
    })
    assert facts == {
        "source": "inline",
        "created_at": "2026-08-29T00:00:00Z",
        "provider_invoked_at": "2026-08-29T00:00:01Z",
        "amount_microusd": 250000,
        "expires_at": 1787958108,
    }


@given(source=st.sampled_from(["inline", "external", "", "INLINE", "inline ", "Inline"]))
def test_only_an_exact_inline_source_counts_as_a_provider_attempt(source):
    """Counting an external hold, or a near-miss spelling, would inflate the number
    that decides how much machinery to build."""
    facts = _reaped_hold_facts({"source": source, "amount_microusd": 1000})
    if source:
        assert facts["source"] == source  # copied verbatim, never normalised
    else:
        assert "source" not in facts
    assert (facts.get("source") == "inline") == (source == "inline")


def test_a_hold_with_no_recognised_facts_yields_an_empty_record():
    """And not a record of Nones, which a reader would mistake for measured zeros."""
    assert _reaped_hold_facts({}) == {}
    assert _reaped_hold_facts({"unrelated": "x"}) == {}


def test_a_non_numeric_amount_is_dropped_rather_than_guessed():
    """A malformed row must not become a zero, which would read as "cost nothing"."""
    facts = _reaped_hold_facts({"source": "inline", "amount_microusd": "not-a-number"})
    assert "amount_microusd" not in facts


# ---------------------------------------------------------------------------
# Wiring: the callers must actually consult these functions
# ---------------------------------------------------------------------------
#
# Both reviews landed on this as the clearest hole: every test above verifies a leaf
# function, and deleting the CALL to it leaves them all green.

def test_the_reaper_records_the_hold_before_deleting_it(dynamodb_mock, monkeypatch):
    """Spy on the projection to prove the reclaim path calls it.

    The evidence exists only because the reclaim copies it out; a reaper that stops
    calling this is the defect, and it is invisible to a unit test of the projection.
    """
    import time

    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period, hold_sk
    from dynamo.user_tenants import UserTenantsRepository
    from mvp import _pipeline

    class _U:
        user_id, org_id, email, roles = "u-wire", "acme-wire", "u@e.com", ("user",)

    period = current_period()
    UserTenantsRepository().ensure(user_id=_U.user_id, tenant_id=_U.org_id,
                                   role="user", total_credit=10**9)
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=_U.org_id, period=period, manual_limit_microusd=10**10)
    ctx = _pipeline.reserve_credit(_U, 4000, pricing_key="opus", cost_microusd=250_000)
    assert ctx.hold_id

    seen = []
    real = _pipeline._reaped_hold_facts
    monkeypatch.setattr(_pipeline, "_reaped_hold_facts",
                        lambda hold: seen.append(dict(hold)) or real(hold))

    budgets = TenantBudgetsRepository()
    item = budgets._table.get_item(
        Key={"tenant_id": _U.org_id, "sk": ctx.hold_sk}).get("Item")
    past = int(time.time()) - 10_000
    budgets._table.delete_item(Key={"tenant_id": _U.org_id, "sk": ctx.hold_sk})
    item["sk"] = hold_sk(period, past, ctx.hold_id)
    item["expires_at"] = past
    budgets._table.put_item(Item=item)
    _pipeline._sweep_expired_holds(budgets, _U.org_id, period)

    assert seen, "the reclaim did not consult the projection — the evidence is lost"
    assert seen[0].get("hold_id") == ctx.hold_id


@pytest.mark.parametrize("route_module", ["anthropic", "chat_completions", "openai_responses"])
def test_every_route_consults_the_classifier_on_failure(route_module, monkeypatch):
    """Every route's hold asks the classifier, and none of them refunds a read
    timeout once the gate is on.

    A route that goes back to refunding on any exception is the original defect,
    and it was the shipped behaviour on eight of the nine endings until the hold
    owned the decision. The spy is on the reference `mvp._money` actually calls,
    so a route that reached past the hold would show up as a missing call rather
    than as a passing test about a symbol nobody uses.
    """
    import importlib

    from mvp import _money
    from mvp._money import run_ending

    route = importlib.import_module(f"mvp.{route_module}")

    seen: list[str] = []
    real = po.classify_exception
    monkeypatch.setattr(_money._outcome, "classify_exception",
                        lambda exc: seen.append(type(exc).__name__) or real(exc))
    monkeypatch.setenv(po.UNOBSERVED_HOLD_ENV, "1")

    refunded: list[int] = []
    released: list[bool] = []

    class _Repo:
        hold_id = "hold-1"

        def refund(self, *, user_id, tenant_id, tokens):
            refunded.append(tokens)

    class _User:
        user_id = "u"
        org_id = "t"

    monkeypatch.setattr(route, "_release_pool", lambda ctx: released.append(True))
    hold = route._open_hold(
        user=_User(), tenants_repo=_Repo(), reservation=4000,
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    # A read timeout by definition happens after the request reached the transport,
    # which every route announces immediately before invoking the provider client. The
    # announcement is what separates a departed call from an exception raised by our own
    # code beforehand, and retention requires it — see `Hold.provider_call_starting`.
    hold.provider_call_starting()
    state = run_ending(hold.claim_unobserved(exc=ReadTimeoutError(endpoint_url="https://x")))

    assert seen == ["ReadTimeoutError"], f"{route_module} did not consult the classifier"
    assert state == po.SUBMITTED_UNSETTLED
    assert refunded == [], f"{route_module} returned a reservation that may have been billed"
    assert released == [], f"{route_module} released a hold it was supposed to retain"

    # The other half of the same route, which is new: the SAME exception without the
    # announcement must be refunded, or a crash inside this gateway holds a tenant's
    # budget until an operator releases it by hand.
    never_left = route._open_hold(
        user=_User(), tenants_repo=_Repo(), reservation=4000,
        model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    run_ending(never_left.claim_unobserved(exc=ReadTimeoutError(endpoint_url="https://x")))
    assert refunded == [4000], (
        f"{route_module} retained a reservation for a request that never reached the "
        f"transport, so it invented a liability rather than recording one")
    assert released == [True], (
        f"{route_module} refunded the token reservation but never released the pool "
        f"hold, so the pool slot is stranded until the reaper")


# ---------------------------------------------------------------------------
# The correlation handle
# ---------------------------------------------------------------------------

@given(hold_id=st.text(min_size=1, max_size=600), tenant=st.text(min_size=1, max_size=600))
@settings(max_examples=200, deadline=None)
def test_request_metadata_stays_inside_the_providers_limits(hold_id, tenant):
    """Bedrock caps values at 256 characters and rejects the whole request when they
    are exceeded, so an over-long id must not turn a billable call into a validation
    error — a request refused for a reason unrelated to the request."""
    md = po.attempt_request_metadata(hold_id, tenant)
    assert all(len(k) <= 256 and len(v) <= 256 for k, v in md.items())
    assert len(md) <= 16  # the provider's per-request entry limit


def test_request_metadata_carries_only_ids_the_gateway_minted():
    """No prompt, no user-supplied text, nothing derived from request content: the
    values land in the operator's invocation logs."""
    md = po.attempt_request_metadata("hold-1", "tenant-1")
    assert md == {"sc_attempt_id": "hold-1", "sc_tenant": "tenant-1"}


def test_no_metadata_without_an_attempt_id():
    """An empty marker is worse than none: it would look like a correlation handle
    while matching every record."""
    assert po.attempt_request_metadata(None) == {}
    assert po.attempt_request_metadata("") == {}
    assert po.attempt_request_metadata(None, "tenant") == {"sc_tenant": "tenant"}
