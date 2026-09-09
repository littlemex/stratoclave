"""HANDOFF-PR4 I5: `mvp.routing.user_dollar_quota.build_grant_revoke_txn_item`,
the per-user revoke builder P4.4 introduces because the existing
`TenantBudgetsRepository.grant_revoke_txn_item` is pool-shaped (it moves three
POOL attributes on the tenant-budgets table) and has no way to address a
`UQ#{period}` row on the model-quotas table at all.

I5 gives the shape VERBATIM, argument names included:

    def build_grant_revoke_txn_item(*, target_pk, target_sk,
                                     approved_amount_microusd, expires_at) -> dict

      UpdateExpression:    ADD granted_microusd :neg SET expires_at = if_not_exists(expires_at, :ttl)
      ConditionExpression: attribute_exists(granted_microusd) AND granted_microusd >= :g

so every test below calls it with the EXACT keywords given -- nothing here is
inferred. Per this split's own instruction that a condition expression's
exact text matters here (moto is known to accept some malformed conditions
real DynamoDB rejects), the literal expression strings are pinned alongside
the behaviour: I5 explicitly forbids `coalesce` in this expression (it is not
a DynamoDB function), and a regression that reintroduced it could otherwise
slip past a behavioural-only check if moto happens to tolerate the syntax.
"""
from __future__ import annotations

import os
from decimal import Decimal

import boto3
import pytest
from botocore.exceptions import ClientError

pytest.importorskip("moto")

_TABLE = os.getenv("DYNAMODB_MODEL_QUOTAS_TABLE", "stratoclave-model-quotas")

TENANT = "acme"
USER = "u1"
PERIOD = "2026-09"


@pytest.fixture
def uq_table(dynamodb_mock):
    return boto3.resource("dynamodb", region_name="us-east-1").Table(_TABLE)


def _client():
    return boto3.client("dynamodb", region_name="us-east-1")


def _keys():
    from mvp.routing.user_dollar_quota import uq_pk, uq_sk

    return uq_pk(TENANT, USER), uq_sk(PERIOD)


def _seed_row(uq_table, *, pk, sk, granted_microusd=None, used=None, expires_at=None):
    item = {"pk": pk, "sk": sk}
    if granted_microusd is not None:
        item["granted_microusd"] = Decimal(granted_microusd)
    if used is not None:
        item["used"] = Decimal(used)
    if expires_at is not None:
        item["expires_at"] = Decimal(expires_at)
    uq_table.put_item(Item=item)


def _get(uq_table, pk, sk):
    return uq_table.get_item(Key={"pk": pk, "sk": sk}).get("Item")


# ---------------------------------------------------------------------------
# The expression text itself
# ---------------------------------------------------------------------------


def test_i5_expression_text_is_exact_and_never_mentions_coalesce():
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=100,
        expires_at=2_000_000_000,
    )
    upd = item["Update"]
    assert upd["UpdateExpression"] == (
        "ADD granted_microusd :neg SET expires_at = if_not_exists(expires_at, :ttl)"
    ), upd["UpdateExpression"]
    assert upd["ConditionExpression"] == (
        "attribute_exists(granted_microusd) AND granted_microusd >= :g"
    ), upd["ConditionExpression"]
    assert "coalesce" not in upd["UpdateExpression"].lower(), (
        "I5, verbatim: 'coalesce is not a DynamoDB function and must not "
        "appear in any expression here.' The design docs' own arithmetic "
        "notation (base + coalesce(granted, 0)) is prose, not an expression "
        "to transcribe literally"
    )
    assert "coalesce" not in upd["ConditionExpression"].lower()


# ---------------------------------------------------------------------------
# The floor, exercised for real
# ---------------------------------------------------------------------------


def test_revoke_returns_exactly_the_grants_amount_and_leaves_used_untouched(uq_table):
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    _seed_row(uq_table, pk=pk, sk=sk, granted_microusd=5_000_000, used=1_000_000)

    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=5_000_000,
        expires_at=9_999_999_999,
    )
    _client().transact_write_items(TransactItems=[item])

    row = _get(uq_table, pk, sk)
    assert int(row["granted_microusd"]) == 0, (
        "the revoke must give back EXACTLY the grant's own amount, driving "
        "granted_microusd to precisely zero, not a partial or over-shot "
        "figure"
    )
    assert int(row["used"]) == 1_000_000, (
        "a revoke moves granted_microusd only -- it must never touch `used`, "
        "which is a live count of what the member has actually spent"
    )


def test_second_revoke_of_the_same_grant_changes_nothing(uq_table):
    """The floor's other half: once `granted_microusd` is at 0, a second
    attempt to subtract the same amount again (a retried early revoke, or a
    sweep racing it) must be refused, never drive the row negative --
    `0 >= :g` is false for any positive `:g`."""
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    _seed_row(uq_table, pk=pk, sk=sk, granted_microusd=3_000_000)
    first = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=3_000_000,
        expires_at=9_999_999_999,
    )
    _client().transact_write_items(TransactItems=[first])
    assert int(_get(uq_table, pk, sk)["granted_microusd"]) == 0

    second = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=3_000_000,
        expires_at=9_999_999_999,
    )
    with pytest.raises(ClientError) as ei:
        _client().transact_write_items(TransactItems=[second])
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert int(_get(uq_table, pk, sk)["granted_microusd"]) == 0, (
        "a refused second revoke must leave the figure exactly where the "
        "first one left it -- not negative, and not decremented a second time"
    )


# ---------------------------------------------------------------------------
# The TTL: created if absent, preserved if present (if_not_exists)
# ---------------------------------------------------------------------------


def test_the_row_still_carries_its_original_ttl_after_the_revoke(uq_table):
    """`if_not_exists(expires_at, :ttl)`: the revoke must not clobber a TTL
    the grant's own APPLY already set. Seed the row with the expiry the
    apply would have written, revoke with a DIFFERENT `expires_at` argument,
    and confirm the ORIGINAL survives -- proving this is `if_not_exists`, not
    a plain `SET` under which the revoke's own idea of the expiry would
    silently win."""
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    original_ttl = 1_800_000_000
    _seed_row(uq_table, pk=pk, sk=sk, granted_microusd=1_000_000, expires_at=original_ttl)

    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=1_000_000,
        expires_at=original_ttl + 999_999,  # deliberately a DIFFERENT value
    )
    _client().transact_write_items(TransactItems=[item])

    row = _get(uq_table, pk, sk)
    assert "expires_at" in row, "the row must still carry a TTL after the revoke"
    assert int(row["expires_at"]) == original_ttl, (
        f"if_not_exists must keep the ORIGINAL expiry ({original_ttl}), not "
        f"the revoke call's own argument ({original_ttl + 999_999}); got "
        f"{row['expires_at']!r}"
    )


def test_revoke_against_a_row_with_no_expires_at_yet_still_sets_one(uq_table):
    """The other side of `if_not_exists`: when nothing has set `expires_at`
    yet, the revoke's own value is what lands -- `if_not_exists` CREATES the
    attribute when it is genuinely absent, it does not merely refuse to
    overwrite."""
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    _seed_row(uq_table, pk=pk, sk=sk, granted_microusd=2_000_000)
    assert "expires_at" not in _get(uq_table, pk, sk)

    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=2_000_000,
        expires_at=1_900_000_000,
    )
    _client().transact_write_items(TransactItems=[item])
    row = _get(uq_table, pk, sk)
    assert int(row["expires_at"]) == 1_900_000_000


# ---------------------------------------------------------------------------
# Absence must fail closed, never subtract into the negative
# ---------------------------------------------------------------------------


def test_revoke_against_a_row_with_no_granted_microusd_at_all_fails_closed(uq_table):
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item

    pk, sk = _keys()
    _seed_row(uq_table, pk=pk, sk=sk, used=500_000)  # a row that has NEVER been granted
    assert "granted_microusd" not in _get(uq_table, pk, sk)

    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=1,
        expires_at=9_999_999_999,
    )
    with pytest.raises(ClientError) as ei:
        _client().transact_write_items(TransactItems=[item])
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"

    row = _get(uq_table, pk, sk)
    assert "granted_microusd" not in row, (
        "I5, verbatim: 'an absent granted_microusd means nothing was "
        "granted, so a revoke against it must fail rather than treat the "
        "absence as zero and subtract.' A version that treated absence as "
        "zero would create a NEGATIVE granted_microusd here"
    )
    assert int(row.get("used", 0)) == 500_000, (
        "an unrelated attribute on the row must be untouched by the refused "
        "write"
    )


def test_revoke_against_a_row_that_does_not_exist_at_all_fails_closed(uq_table):
    """The stronger form of the same guard: no row at ALL, not merely a row
    with the attribute absent. `attribute_exists` on a nonexistent item fails
    the same way, and the write must create no phantom row."""
    from mvp.routing.user_dollar_quota import build_grant_revoke_txn_item, uq_pk, uq_sk

    pk, sk = uq_pk("ghost-tenant", "ghost-user"), uq_sk("2026-09")
    item = build_grant_revoke_txn_item(
        target_pk=pk, target_sk=sk, approved_amount_microusd=1,
        expires_at=9_999_999_999,
    )
    with pytest.raises(ClientError) as ei:
        _client().transact_write_items(TransactItems=[item])
    assert ei.value.response["Error"]["Code"] == "TransactionCanceledException"
    assert _get(uq_table, pk, sk) is None, (
        "a refused revoke against a nonexistent row must create no phantom "
        "row"
    )
