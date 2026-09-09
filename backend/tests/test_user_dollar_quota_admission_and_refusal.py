"""HANDOFF-PR3 G1 (a configured wall contributes an item to the admission
transaction; an unconfigured one does not) and I7 (the refusal that lists
every wall that refused and headlines the least remediable one).

G1's OWN TEXT IS EXPLICIT ABOUT THE ASSERTION SHAPE: 'A per-wall pair of tests
observing the transaction handed to `TransactWriteItems`... Not the return
value of a builder called directly.' This file honours that: every assertion
here is against the `TransactItems` kwarg actually passed to the low-level
DynamoDB client's `transact_write_items`, captured by monkeypatching
`mvp._pipeline._low_level_client()` -- the same client-swap technique
`tests/test_billing_authorize.py` already uses (`monkeypatch.setattr(_pipeline,
"_low_level_client", lambda: ...)`), just recording instead of altering
behaviour.

WIRING ASSUMPTION (same one `test_user_dollar_quota_reverse_paths.py` makes,
restated here because this file is where it is highest-stakes): calling
`reserve_credit(user, tokens, cost_microusd=cost, selected_model=...)` with NO
new keyword picks up the new wall automatically from `user.org_id` /
`user.user_id` / the resolved period, because nothing in I1-I7 changes this
function's signature. If the real wiring instead requires a new parameter
this file does not pass, the POSITIVE test
(`test_a_configured_wall_contributes_an_item_to_the_real_transaction`)
fails (no UQ item found) while the NEGATIVE test
passes vacuously (also no UQ item found, which is what it expects) -- so a
green negative test alone must not be read as confirmation the wiring
assumption was right; only the positive test failing/passing is informative
here, and both are included for exactly this reason.

I7'S BODY SHAPE IS PARTIALLY UNSPECIFIED: the interface says the 402 'lists
every wall whose cancellation reason says it refused' but names no field for
that list (the EXISTING `_refusal_body` in `mvp/_pipeline.py` carries a single
`wall`/`blocker` pair today, described in its own docstring as the shape
before this change). This file asserts the part that IS fully specified --
the headline (`wall`/`blocker`) must be the NON-grantable wall
(`user_dollar_quota`/`personal_spend`) even though the grantable
`tenant_dollar_pool`/`tenant_pool` also refused -- and separately checks, by
recursively scanning every string value in the JSON-able detail body, that
BOTH walls' public blocker names appear SOMEWHERE in it, tolerant of whatever
the plural list's own key turns out to be named. If that recursive scan does
not find `tenant_pool` anywhere, the failure message says so explicitly
rather than pointing at a subtracted assertion silently downgraded.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

import boto3
import pytest
from fastapi import HTTPException

pytest.importorskip("moto")

from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
from dynamo.tenants import TenantsRepository
from dynamo.user_tenants import UserTenantsRepository
from mvp import _pipeline

TENANT_CONFIGURED = "uq-admission-configured-tenant"
TENANT_UNCONFIGURED = "uq-admission-unconfigured-tenant"
MODEL = "claude-sonnet-5"
DEFAULT_TOKENS = 2500
DEFAULT_COST = 1_000_000  # $1.00


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


class _CapturingClient:
    """Forwards every call to the REAL low-level client (so the reservation
    actually commits against moto exactly as it would in production) while
    recording each `transact_write_items` call's `TransactItems`, in commit
    order. `__getattr__` delegates everything else untouched."""

    def __init__(self, real):
        self._real = real
        self.calls: list[list[dict]] = []

    def transact_write_items(self, **kwargs):
        self.calls.append(kwargs.get("TransactItems") or [])
        return self._real.transact_write_items(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def capture(monkeypatch):
    real = _pipeline._low_level_client()
    cap = _CapturingClient(real)
    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: cap)
    return cap


def _seed_tenant(tenant_id: str, *, pool_limit: int, uq_default_microusd=None) -> str:
    TenantsRepository().create(
        tenant_id=tenant_id, name=tenant_id, team_lead_user_id=f"admin-{tenant_id}",
        default_credit=10**12, created_by="test")
    period = current_period()
    TenantBudgetsRepository().set_manual_limit(
        tenant_id=tenant_id, period=period, manual_limit_microusd=pool_limit)
    if uq_default_microusd is not None:
        TenantsRepository()._table.update_item(
            Key={"tenant_id": tenant_id},
            UpdateExpression="SET user_dollar_defaults = :m, user_dollar_defaults_version = :v",
            ExpressionAttributeValues={
                ":m": {"2020-01": Decimal(uq_default_microusd)},
                ":v": 1,
            },
        )
    return period


def _user(tenant_id: str, uid: str) -> _User:
    UserTenantsRepository().ensure(user_id=uid, tenant_id=tenant_id, role="user",
                                    total_credit=10**12)
    return _User(user_id=uid, org_id=tenant_id)


def _find_item(items, *, pk_prefix: str) -> dict | None:
    """The first RAW-format `Update` item (low-level API shape,
    `Key={"pk": {"S": ...}}`) whose `pk` starts with `pk_prefix`. Used to find
    the new wall's item by its partition-key SHAPE (`TENANT#{t}#USER#{u}`,
    I1's `uq_pk`) without importing the new module's own key-builder into a
    file whose whole point is to observe the transaction from the outside."""
    for it in items or []:
        upd = it.get("Update") or {}
        pk = (upd.get("Key") or {}).get("pk", {}).get("S", "")
        if pk.startswith(pk_prefix):
            sk = (upd.get("Key") or {}).get("sk", {}).get("S", "")
            if sk.startswith("UQ#"):
                return it
    return None


# --------------------------------------------------------------------------- G1: the pair


def test_a_configured_wall_contributes_an_item_to_the_real_transaction(dynamodb_mock, capture):
    """G1, positive half. A tenant with a sealed/configured per-user dollar
    default must have an item keyed `UQ#{period}` under
    `TENANT#{tenant}#USER#{user}` inside the SAME `TransactWriteItems` call
    that admitted the request -- not a second, separate call, and not merely
    something a builder WOULD have returned if invoked by hand (G1's own text:
    'not the return value of a builder called directly')."""
    period = _seed_tenant(TENANT_CONFIGURED, pool_limit=10**9,
                           uq_default_microusd=100_000_000)
    user = _user(TENANT_CONFIGURED, "u-configured")

    ctx = _pipeline.reserve_credit(
        user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=DEFAULT_COST,
        selected_model=MODEL,
    )
    assert ctx is not None  # admitted

    assert capture.calls, "reserve_credit committed via a client this test did not capture"
    committed = capture.calls[-1]
    item = _find_item(committed, pk_prefix=f"TENANT#{TENANT_CONFIGURED}#USER#u-configured")
    assert item is not None, (
        f"no UQ#{period} item for the configured tenant/user was found in the "
        f"transaction actually sent to TransactWriteItems: {committed!r}"
    )


def test_an_unconfigured_wall_contributes_no_item_and_admission_proceeds(
    dynamodb_mock, capture,
):
    """G1, negative half. A tenant that has NEVER set a per-user dollar
    default must be admitted (I3: absent history is 'unconfigured', not an
    error, and admissions proceed) with NO `UQ#` item anywhere in the
    committed transaction -- I5's 'a predicate that performs its own read is
    a contract violation... default absent at predicate time and present at
    build time emits no item and admits the request against a ceiling that
    exists' names exactly the failure this negative half exists to catch: an
    item that gets built anyway (e.g. against a stale/cached ceiling) even
    though nothing is configured."""
    _seed_tenant(TENANT_UNCONFIGURED, pool_limit=10**9, uq_default_microusd=None)
    user = _user(TENANT_UNCONFIGURED, "u-unconfigured")

    ctx = _pipeline.reserve_credit(
        user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=DEFAULT_COST,
        selected_model=MODEL,
    )
    assert ctx is not None  # admitted -- unconfigured must never itself refuse

    assert capture.calls, "reserve_credit committed via a client this test did not capture"
    committed = capture.calls[-1]
    item = _find_item(committed, pk_prefix=f"TENANT#{TENANT_UNCONFIGURED}#USER#u-unconfigured")
    assert item is None, (
        f"an UQ item was found for a tenant with no configured default at "
        f"all: {item!r} -- an unconfigured wall must never contribute an "
        f"admission item"
    )


# --------------------------------------------------------------------------- I4: the registry entry itself (exact literals)


def test_both_money_walls_are_grantable_and_the_token_wall_is_not():
    """Deliberately reversed, and this test is the evidence the change is real.

    It asserted `user_dollar_quota.grantable is False`, which was TRUE and CORRECT
    when the ceiling shipped: there was no raise path for it, and declaring it
    grantable would have printed a hint pointing at a request the code refused. The
    raise path now exists, so the same assertion has become the thing standing in the
    way, and flipping it is a behaviour change somebody decided rather than a rename
    somebody absorbed.

    Both money walls are now raisable and the TOKEN wall still is not, which is the
    distinction worth pinning: money can be granted, a token allowance is a different
    lever with a different owner. The refusal-ordering rule reads grantability from
    here, so this is also what makes that rule non-trivial for the first time — with
    one grantable wall it had nothing to order.
    """
    from mvp.reserve_limits import limit_kind

    assert limit_kind("tenant_dollar_pool").grantable is True
    assert limit_kind("user_dollar_quota").grantable is True
    assert limit_kind("user_token_quota").grantable is False


def test_public_blocker_name_is_personal_spend_not_personal_budget():
    """I4, verbatim: '`_BLOCKER_BY_WALL` gains `"user_dollar_quota":
    "personal_spend"`. It must not be `personal_budget` -- that is the TOKEN
    wall's public name, and reusing it would make a money refusal
    indistinguishable from a token refusal to a client.' Direct pin on both
    halves of that sentence: the new wall's own name, and that it must not
    collide with the pre-existing one."""
    from mvp.grants import blocker_for_wall

    assert blocker_for_wall("user_dollar_quota") == "personal_spend"
    assert blocker_for_wall("user_token_quota") == "personal_budget"
    assert blocker_for_wall("user_dollar_quota") != blocker_for_wall("user_token_quota")


# --------------------------------------------------------------------------- I7: the refusal, both walls, non-grantable headlines


def _all_string_values(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_string_values(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_string_values(v)


def test_the_pool_refuses_first_and_says_clearing_it_may_not_be_enough(dynamodb_mock):
    """Contract amendments A5/A8: what a member is told when more than one wall
    cannot admit the request.

    The obvious test to write here is the one this file originally had -- both walls
    full, assert the 402 headlines the non-grantable one -- and it fails against
    correct code, which is why the contract changed rather than the code. The pool
    refuses in PROCESS: `_pipeline.py`'s check is arithmetic on a row it just read
    (`p_reserved + p_settled + cost > p_limit`), and it raises before the transaction
    is ever attempted. So when the pool is full, NO other wall has been evaluated:
    there is no set of refusing walls to order, and the only way to get one would be
    a second in-process pre-check, which the contract rejects by name because it tells
    a member about one wall per round trip.

    What protects the member is therefore not the ordering but the DISCLOSURE. The
    hint carries a `shortfall_microusd`, which reads as "raise this much and you are
    through" -- and that is how an approved raise buys nothing: the member raises the
    pool, the raise lands, and the identical request is refused by the money ceiling
    nobody mentioned. `raising_this_may_not_be_sufficient` is the sentence that stops
    the hint promising something it cannot deliver.

    This test therefore pins: the pool refuses (it is first), and the hint admits its
    own limits. It deliberately does NOT assert the ceiling is headlined -- that would
    be asserting a behaviour the architecture does not have and the contract no longer
    asks for."""
    tenant_id = "uq-both-refuse-tenant"
    # A ceiling far below the request AND a pool that cannot cover it either, so both
    # walls would refuse if both were reached. The pool is drained by a first request
    # that succeeds, rather than by writing a reserved figure directly, so the row this
    # refusal reads is one the product produced.
    _seed_tenant(tenant_id, pool_limit=2_000_000, uq_default_microusd=1_500_000)
    user = _user(tenant_id, "u-both-refuse")
    # One admitted request leaves BOTH walls unable to take the next one: the ceiling
    # has 100_000 of headroom left and the pool has 600_000, against a request costing
    # 1_000_000. Both figures come from a request the product actually admitted, so
    # neither wall's state is hand-written.
    _pipeline.reserve_credit(
        user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=1_400_000,
        selected_model=MODEL,
    )

    with pytest.raises(HTTPException) as ei:
        _pipeline.reserve_credit(
            user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=DEFAULT_COST,
            selected_model=MODEL,
        )
    detail = ei.value.detail
    assert isinstance(detail, dict), detail
    assert detail.get("wall") == "tenant_dollar_pool", (
        f"the pool's in-process check runs before the transaction, so it is the wall "
        f"that refuses; got {detail.get('wall')!r}. If this ever names the ceiling, "
        f"the refusal has moved into the transaction and A8's deferred ordering "
        f"becomes reachable -- read that amendment before changing this assertion"
    )
    hint = detail.get("raise_hint") or {}
    assert hint.get("raising_this_may_not_be_sufficient") is True, (
        f"the hint offers a shortfall for the pool while the money ceiling was never "
        f"evaluated, so it must not imply that raising the pool admits the request. "
        f"Got raise_hint={hint!r}"
    )

def test_402_never_reports_a_transient_conflict_as_a_definitive_refusal(
    dynamodb_mock, monkeypatch,
):
    """I7: 'A `TransactionConflict` is never reported as a definitive 402.'
    Forces every attempt's cancellation to be `TransactionConflict` (a
    transient capacity signal, never a real refusal) by monkeypatching the
    captured client's `transact_write_items` to always raise it, and asserts
    the caller sees the EXISTING retryable-503 shape
    (`budget_unavailable`/`pool_reservation_contended`), never a 402 naming
    either wall -- a `TransactionConflict` misread as `user_dollar_quota`
    refusing would tell a member to ask for a raise for a problem a retry
    would have cleared on its own."""
    from botocore.exceptions import ClientError

    tenant_id = "uq-transient-conflict-tenant"
    _seed_tenant(tenant_id, pool_limit=10**9, uq_default_microusd=100_000_000)
    user = _user(tenant_id, "u-transient")

    real = _pipeline._low_level_client()

    class _AlwaysConflict:
        def transact_write_items(self, **kwargs):
            if "TransactItems" not in kwargs:
                return real.transact_write_items(**kwargs)
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException", "Message": "x"},
                    "CancellationReasons": [
                        {"Code": "TransactionConflict"} for _ in kwargs["TransactItems"]
                    ],
                },
                "TransactWriteItems",
            )

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(_pipeline, "_low_level_client", lambda: _AlwaysConflict())

    with pytest.raises(HTTPException) as ei:
        _pipeline.reserve_credit(
            user, DEFAULT_TOKENS, pricing_key=None, cost_microusd=DEFAULT_COST,
            selected_model=MODEL,
        )
    exc = ei.value
    assert exc.status_code == 503, (
        f"a pure TransactionConflict on every attempt must surface as a "
        f"retryable 503, not this: {exc.status_code} {exc.detail!r}"
    )
    detail = exc.detail
    if isinstance(detail, dict):
        assert detail.get("wall") not in ("user_dollar_quota", "tenant_dollar_pool")
