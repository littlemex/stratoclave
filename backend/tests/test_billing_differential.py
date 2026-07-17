"""LIVE differential test: real _pipeline (REAL DynamoDB) vs billing/ledger.py.

Fable's deepest verification layer. The Z3 proofs + moto stateful test reason
over a MODEL (billing/ledger.py); moto cannot enforce the two things that
actually bit us (ClientRequestToken 36-char limit; real TransactWriteItems
condition/serialise semantics). This drives the SAME op sequence through the
REAL reserve/settle/release/reaper code on throwaway `sc-diff-*` tables in REAL
DynamoDB and asserts the pool counters agree with the ledger after every step.
It mechanically catches F1 (settled-only token >36 chars) and F2 (overshoot).

SKIPPED BY DEFAULT (hits real AWS, ~2 min). Opt in pre-merge:

    SC_DIFF_LIVE=1 AWS_PROFILE=claude-code AWS_REGION=us-east-1 \
        pytest tests/test_billing_differential.py -m live -q

Safe: only `sc-diff-*` tables (triple-guarded teardown); no Bedrock.
"""
import pytest

pytestmark = pytest.mark.live

if __import__("os").getenv("SC_DIFF_LIVE") != "1":
    pytest.skip("live differential harness: set SC_DIFF_LIVE=1 to run",
                allow_module_level=True)

import os
import sys
import uuid

os.environ["AWS_REGION"] = "us-east-1"
_PREFIX = "sc-diff"
os.environ["DYNAMODB_USER_TENANTS_TABLE"] = f"{_PREFIX}-user-tenants"
os.environ["DYNAMODB_TENANT_BUDGETS_TABLE"] = f"{_PREFIX}-tenant-budgets"
os.environ["DYNAMODB_USAGE_LOGS_TABLE"] = f"{_PREFIX}-usage-logs"
os.environ["DYNAMODB_PRICING_CONFIG_TABLE"] = f"{_PREFIX}-pricing-config"
# Ledger Phase 2: reserve/settle/release now co-write ledger events, so the
# reserve txn references the credit-ledger table — the harness must provision it
# or every reserve ResourceNotFounds.
os.environ["DYNAMODB_CREDIT_LEDGER_TABLE"] = f"{_PREFIX}-credit-ledger"

import boto3  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from dataclasses import dataclass  # noqa: E402

# conftest.py forces DUMMY creds (AWS_ACCESS_KEY_ID=testing) + deletes
# AWS_PROFILE at import so the rest of the suite can never touch real AWS. This
# LIVE test must undo that: hydrate real credentials from the requested profile
# and write them into the environment BEFORE the backend builds its cached
# DynamoDB resource, so both our client and the repo code authenticate for real.
def _hydrate_real_credentials():
    profile = os.getenv("SC_DIFF_AWS_PROFILE") or os.getenv("AWS_PROFILE") or "claude-code"
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        os.environ.pop(k, None)  # drop conftest's dummies
    sess = boto3.Session(profile_name=profile, region_name="us-east-1")
    creds = sess.get_credentials()
    if creds is None:
        import pytest as _pt
        _pt.skip(f"no AWS credentials for profile {profile!r}", allow_module_level=True)
    frozen = creds.get_frozen_credentials()
    os.environ["AWS_ACCESS_KEY_ID"] = frozen.access_key
    os.environ["AWS_SECRET_ACCESS_KEY"] = frozen.secret_key
    if frozen.token:
        os.environ["AWS_SESSION_TOKEN"] = frozen.token
    return sess


_session = _hydrate_real_credentials()
_client = _session.client("dynamodb", region_name="us-east-1")


@dataclass
class _User:
    user_id: str
    org_id: str
    email: str = "u@example.com"


def _create_table(name, key_schema, attr_defs):
    try:
        _client.create_table(
            TableName=name, KeySchema=key_schema,
            AttributeDefinitions=attr_defs, BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise


def _setup_tables():
    _create_table(
        os.environ["DYNAMODB_USER_TENANTS_TABLE"],
        [{"AttributeName": "user_id", "KeyType": "HASH"},
         {"AttributeName": "tenant_id", "KeyType": "RANGE"}],
        [{"AttributeName": "user_id", "AttributeType": "S"},
         {"AttributeName": "tenant_id", "AttributeType": "S"}],
    )
    _create_table(
        os.environ["DYNAMODB_TENANT_BUDGETS_TABLE"],
        [{"AttributeName": "tenant_id", "KeyType": "HASH"},
         {"AttributeName": "sk", "KeyType": "RANGE"}],
        [{"AttributeName": "tenant_id", "AttributeType": "S"},
         {"AttributeName": "sk", "AttributeType": "S"}],
    )
    _create_table(
        os.environ["DYNAMODB_USAGE_LOGS_TABLE"],
        [{"AttributeName": "tenant_id", "KeyType": "HASH"},
         {"AttributeName": "timestamp_log_id", "KeyType": "RANGE"}],
        [{"AttributeName": "tenant_id", "AttributeType": "S"},
         {"AttributeName": "timestamp_log_id", "AttributeType": "S"}],
    )
    # Credit ledger (Phase 2): pk/sk + the run-index GSI the event writers set.
    try:
        _client.create_table(
            TableName=os.environ["DYNAMODB_CREDIT_LEDGER_TABLE"],
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"},
                       {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "run-index",
                "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"},
                              {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceInUseException":
            raise
    for k in ("DYNAMODB_USER_TENANTS_TABLE", "DYNAMODB_TENANT_BUDGETS_TABLE",
              "DYNAMODB_USAGE_LOGS_TABLE", "DYNAMODB_CREDIT_LEDGER_TABLE"):
        _client.get_waiter("table_exists").wait(TableName=os.environ[k])


def _teardown_tables():
    for k in ("DYNAMODB_USER_TENANTS_TABLE", "DYNAMODB_TENANT_BUDGETS_TABLE",
              "DYNAMODB_USAGE_LOGS_TABLE", "DYNAMODB_CREDIT_LEDGER_TABLE"):
        name = os.environ[k]
        assert name.startswith(_PREFIX), f"refusing to delete {name}"
        try:
            _client.delete_table(TableName=name)
        except ClientError:
            pass


def _run_differential():
    from hypothesis import settings, HealthCheck
    from hypothesis import strategies as st
    from hypothesis.stateful import (
        Bundle, RuleBasedStateMachine, consumes, initialize, invariant, multiple, rule,
    )

    from billing.ledger import BillingLedger
    from mvp import _pipeline
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period
    from dynamo.user_tenants import UserTenantsRepository

    TENANT = "diff-org"
    PERIOD = current_period()
    budgets = TenantBudgetsRepository()

    def real_pool():
        s = budgets.pool_summary(TENANT, PERIOD)
        return int(s["pool_reserved_microusd"]), int(s["pool_settled_microusd"])

    AMOUNTS = st.integers(min_value=1, max_value=100_000)      # micro-USD
    USAGE = st.integers(min_value=0, max_value=200_000)        # allow overshoot (F2)
    LIMITS = st.integers(min_value=100_000, max_value=5_000_000)

    class DiffMachine(RuleBasedStateMachine):
        holds = Bundle("holds")

        @initialize(limit=LIMITS)
        def init(self, limit):
            # fresh user per run so per-user token cap never gates the pool test
            self.user = _User(user_id=f"u-{uuid.uuid4()}", org_id=TENANT)
            UserTenantsRepository().ensure(
                user_id=self.user.user_id, tenant_id=TENANT,
                role="user", total_credit=10**12,
            )
            # Reset the pool row to CLEAN counters for this example. set_pool_limit
            # is a *preserving* put (keeps reserved/settled), so across examples
            # the shared sc-diff table would accumulate — put an explicit
            # zero-counter row instead so every example starts from R=S=0.
            budgets._table.put_item(Item={
                "tenant_id": TENANT, "sk": f"BUDGET#{PERIOD}",
                "pool_limit_microusd": limit,
                "pool_reserved_microusd": 0,
                "pool_settled_microusd": 0,
                "status": "active", "version": "1",
            })
            # Also purge any leftover HOLD rows from a prior example.
            from boto3.dynamodb.conditions import Key as _Key
            resp = budgets._table.query(
                KeyConditionExpression=_Key("tenant_id").eq(TENANT)
                & _Key("sk").begins_with("HOLD#"))
            for it in resp.get("Items", []):
                budgets._table.delete_item(
                    Key={"tenant_id": TENANT, "sk": it["sk"]})
            # reference model + our own bookkeeping (starts clean at 0)
            self.ledger = BillingLedger(limit=limit)
            self.limit = limit
            self.ctxs = {}          # hold_key -> ReservationContext
            self.live = {}          # hold_key -> reserved cost
            self.overshoot_debt = 0
            self.max_limit_seen = limit
            self._check()

        def _check(self):
            rr, rs = real_pool()
            lr, ls = self.ledger.reserved(), self.ledger.settled_total()
            assert rr == lr, f"reserved diverged: real={rr} ledger={lr}"
            assert rs == ls, f"settled diverged: real={rs} ledger={ls}"
            assert rr >= 0, f"real reserved negative: {rr}"
            # Ceiling with honest overshoot: R+S may exceed the CURRENT limit
            # only up to (a) the highest limit ever set — admin lowering L below
            # committed usage doesn't claw back — plus (b) accumulated overshoot
            # (settle actual>reserved, reaper-race spend). Both are monotonic, so
            # this can't be spuriously reset by shrinking.
            assert rr + rs <= self.max_limit_seen + self.overshoot_debt, (
                f"ceiling breached: R={rr} S={rs} max_L_seen={self.max_limit_seen} "
                f"overshoot={self.overshoot_debt}")

        @rule(target=holds, cost=AMOUNTS)
        def reserve(self, cost):
            rr, rs = real_pool()
            fits = rr + rs + cost <= self.limit
            if not fits:
                # real code must 402; ledger must LimitExceeded — both reject
                from billing.ledger import LimitExceeded
                try:
                    _pipeline.reserve_credit(self.user, cost, pricing_key="opus",
                                             cost_microusd=cost)
                    raise AssertionError("real reserve admitted past ceiling")
                except Exception as e:
                    if "402" not in str(e) and "exhaust" not in str(e).lower():
                        raise
                try:
                    self.ledger.reserve(cost)
                    raise AssertionError("ledger admitted past ceiling")
                except LimitExceeded:
                    pass
                self._check()
                return multiple()
            ctx = _pipeline.reserve_credit(self.user, cost, pricing_key="opus",
                                           cost_microusd=cost)
            hk = self.ledger.reserve(cost)
            self.ctxs[hk] = ctx
            self.live[hk] = cost
            self._check()
            return hk

        @rule(hk=consumes(holds), actual=USAGE)
        def settle(self, hk, actual):
            reserved = self.live.pop(hk)
            _pipeline.settle_reservation_and_log(
                user=self.user, tenants_repo=self.ctxs[hk],
                reservation=0, actual_input_tokens=0, actual_output_tokens=0,
                model_id="diff", context=self.ctxs[hk],
                actual_cost_microusd=actual,
            )
            self.ledger.settle(hk, actual)
            self.overshoot_debt += max(0, actual - reserved)
            del self.ctxs[hk]
            self._check()

        @rule(hk=consumes(holds))
        def release(self, hk):
            self.live.pop(hk)
            _pipeline.release_pool(self.ctxs[hk])
            self.ledger.release(hk)
            del self.ctxs[hk]
            self._check()

        @rule(hk=consumes(holds), actual=USAGE)
        def crash_then_reap(self, hk, actual):
            """Reaper-race: delete this hold's HOLD row (as the reaper would,
            returning reserved) BEFORE settling → real settle hits hold_gone →
            settled-only fallback (the F1 path). Mirror in the ledger by reaping
            (returns reserved, no spend) — so the real settled-only spend is the
            EXTRA the harness watches: real S grows by `actual`, ledger by 0, so
            they'd diverge unless we also record it in the ledger as a post-reap
            settle-of-spend. We model it as: reap frees reserved, then the
            fallback records `actual` spend.
            """
            reserved = self.live.pop(hk)
            ctx = self.ctxs.pop(hk)
            # delete the hold row directly (reaper's reclaim already returned
            # reserved to the pool; do that too to match the reaper).
            sk = ctx.hold_sk
            budgets._table.update_item(
                Key={"tenant_id": TENANT, "sk": f"BUDGET#{PERIOD}"},
                UpdateExpression="ADD pool_reserved_microusd :d",
                ExpressionAttributeValues={":d": -reserved},
            )
            budgets._table.delete_item(Key={"tenant_id": TENANT, "sk": sk})
            # ledger: reaper frees reserved
            self.ledger.expire_lease(hk)
            self.ledger.reap_expired()
            # now settle → real code hits hold_gone → settled-only records spend
            _pipeline.settle_reservation_and_log(
                user=self.user, tenants_repo=ctx, reservation=0,
                actual_input_tokens=0, actual_output_tokens=0,
                model_id="diff", context=ctx, actual_cost_microusd=actual,
            )
            # mirror the fallback spend in the ledger (settled-only = +actual)
            self.ledger._settled += actual
            self.overshoot_debt += actual  # spend recorded with reserved already freed
            self._check()

        @rule(new_limit=LIMITS)
        def set_limit(self, new_limit):
            budgets.set_pool_limit(tenant_id=TENANT, period=PERIOD,
                                   pool_limit_microusd=new_limit)
            self.ledger.set_limit(new_limit)
            self.limit = new_limit
            self.max_limit_seen = max(self.max_limit_seen, new_limit)
            self._check()

        @invariant()
        def counters_agree(self):
            self._check()

    DiffMachine.TestCase.settings = settings(
        max_examples=int(os.getenv("DIFF_EXAMPLES", "15")),
        stateful_step_count=int(os.getenv("DIFF_STEPS", "12")),
        deadline=None,
        suppress_health_check=list(HealthCheck),
    )

    def deterministic_reaper_race():
        """Directly exercise the F1 path (reserve -> reaper reclaims the hold ->
        settle hits hold_gone -> settled-only fallback records spend). This is
        NOT left to Hypothesis's random draw, so a token-length regression is
        caught EVERY run. Asserts the settled-only spend actually landed.
        """
        user = _User(user_id=f"u-reap-{uuid.uuid4()}", org_id=TENANT)
        UserTenantsRepository().ensure(user_id=user.user_id, tenant_id=TENANT,
                                       role="user", total_credit=10**12)
        budgets.set_pool_limit(tenant_id=TENANT, period=PERIOD,
                               pool_limit_microusd=5_000_000)
        cost, actual = 40_000, 30_000
        r0, s0 = real_pool()
        ctx = _pipeline.reserve_credit(user, cost, pricing_key="opus",
                                       cost_microusd=cost)
        # reaper reclaims: return reserved to the pool + delete the HOLD row.
        budgets._table.update_item(
            Key={"tenant_id": TENANT, "sk": f"BUDGET#{PERIOD}"},
            UpdateExpression="ADD pool_reserved_microusd :d",
            ExpressionAttributeValues={":d": -cost},
        )
        budgets._table.delete_item(Key={"tenant_id": TENANT, "sk": ctx.hold_sk})
        # settle now hits hold_gone -> settled-only fallback. With a 39-char
        # token this raises ValidationException (F1); with the fix it records
        # `actual` into pool_settled.
        _pipeline.settle_reservation_and_log(
            user=user, tenants_repo=ctx, reservation=0,
            actual_input_tokens=0, actual_output_tokens=0,
            model_id="diff", context=ctx, actual_cost_microusd=actual,
        )
        r1, s1 = real_pool()
        assert s1 - s0 == actual, (
            f"settled-only did not record spend on the reaper-race path: "
            f"expected +{actual}, got +{s1 - s0} (F1: token>36 chars → "
            f"ValidationException → silent revenue leak)")
        assert r1 == r0, f"reserved not restored: r0={r0} r1={r1}"

    def deterministic_external_authorize_capture():
        """Exercise the NEW external authcap money path on REAL DynamoDB (P0):
        authorize (pool CAS + HOLD + RESERVE(source=external) + IDEMP, one txn) →
        idempotent replay (same key, no second hold) → rehydrate → capture
        (unmodified _settle_pool_side). Asserts the pool math and that a duplicate
        key reserves exactly once — the IDEMP row + external settle are the only
        new money writes, so they get live differential coverage too."""
        from mvp.billing_authorize import (
            decode_authorization_id, encode_authorization_id, _settle_external,
        )

        tenant = TENANT
        budgets.set_pool_limit(tenant_id=tenant, period=PERIOD,
                               pool_limit_microusd=5_000_000)
        # clean counters for this check
        budgets._table.put_item(Item={
            "tenant_id": tenant, "sk": f"BUDGET#{PERIOD}",
            "pool_limit_microusd": 5_000_000,
            "pool_reserved_microusd": 0, "pool_settled_microusd": 0,
            "status": "active", "version": "1",
        })
        r0, s0 = real_pool()
        key = f"diff-authcap-{uuid.uuid4()}"
        mk = lambda h, p, sk: encode_authorization_id(hold_id=h, period=p, hold_sk=sk)
        res = _pipeline.reserve_external_authorization(
            tenant_id=tenant, amount_microusd=80_000, idempotency_key=key,
            request_fingerprint="fp", authorization_id_factory=mk, ttl_seconds=3600,
        )
        r1, _ = real_pool()
        assert r1 - r0 == 80_000, f"authorize did not reserve 80000: {r1 - r0}"
        # idempotent replay: same key → same id, no second reserve.
        res2 = _pipeline.reserve_external_authorization(
            tenant_id=tenant, amount_microusd=80_000, idempotency_key=key,
            request_fingerprint="fp", authorization_id_factory=mk, ttl_seconds=3600,
        )
        assert res2.authorization_id == res.authorization_id and res2.replayed
        r2, _ = real_pool()
        assert r2 == r1, f"duplicate key double-reserved: r1={r1} r2={r2}"
        # capture 50000 via the unmodified settle.
        hold_id, per, hold_sk = decode_authorization_id(res.authorization_id)
        ctx = _pipeline.rehydrate_reservation_context(
            tenant_id=tenant, period=per, hold_id=hold_id, hold_sk=hold_sk)
        assert ctx is not None, "rehydrate returned None for a live external hold"
        _settle_external(ctx, 50_000)
        r3, s3 = real_pool()
        assert r3 == r0 and s3 - s0 == 50_000, (
            f"external capture math wrong: reserved back to {r3} (want {r0}), "
            f"settled +{s3 - s0} (want 50000)")

    import unittest
    print(f"account/region: us-east-1, prefix: {_PREFIX}-*  period={PERIOD}")
    print("setting up throwaway tables...")
    _setup_tables()
    ok = True
    try:
        print("\n[1/3] deterministic reaper-race (F1 guard)...")
        try:
            deterministic_reaper_race()
            print("  reaper-race PASS: settled-only recorded spend, token <=36 ok")
        except Exception as e:
            ok = False
            print(f"  reaper-race FAIL: {type(e).__name__}: {e}")
        print("\n[2/3] deterministic external authorize/capture (authcap money path)...")
        try:
            deterministic_external_authorize_capture()
            print("  authcap PASS: reserve+IDEMP idempotent, external capture math ok")
        except Exception as e:
            ok = False
            print(f"  authcap FAIL: {type(e).__name__}: {e}")
        print("\n[3/3] randomized differential state machine...")
        suite = unittest.TestLoader().loadTestsFromTestCase(DiffMachine.TestCase)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        ok = ok and result.wasSuccessful()
    finally:
        print("tearing down throwaway tables...")
        _teardown_tables()
    print("RESULT:", "PASS" if ok else "FAIL")
    assert ok, 'differential harness failed'





def test_billing_differential_against_real_dynamodb():
    _run_differential()
