"""Two different Idempotency-Keys are two different operations.

The sort key for an external-authorize idempotency record used to be the client's
key with every character outside `[A-Za-z0-9._-]` replaced by `_`, truncated at 512.
Its docstring argued the collapse was fail-safe, because merging two keys can only
dedupe more aggressively and never produce a second authorize. That is true about
double-charging and false about the operation the caller actually asked for:
`invoice/a` and `invoice?a` landed on one cell, so the second authorize either
replayed the first — answering "already done" for an amount that was never held — or,
when the bodies differed, was refused as a reused key. Neither is a double charge;
both are a wrong answer to a well-formed request.

The key is now addressed by a digest of the raw bytes, with the raw key kept in the
row so a replay can verify the key itself. Rows written before that are still found,
because a retry of an old key must not mint a second hold.
"""
from __future__ import annotations

import pytest

from dynamo.credit_ledger import (
    _idemp_token,
    _legacy_safe_idemp_token,
    idemp_sk,
    legacy_idemp_sk,
)

#: Pairs that the sanitising token collapsed onto one cell. Each is a plausible
#: client key shape: a path, a query, a URL-ish reference, a scoped id.
COLLIDING_PAIRS = [
    ("invoice/a", "invoice?a"),
    ("run:1", "run 1"),
    ("job#7", "job%7"),
    ("tenant/acme/op", "tenant?acme?op"),
]


@pytest.mark.parametrize("left,right", COLLIDING_PAIRS)
def test_keys_the_old_token_merged_are_now_distinct(left, right):
    assert _legacy_safe_idemp_token(left) == _legacy_safe_idemp_token(right), (
        "the premise of the test: these keys used to share one idempotency cell"
    )
    assert idemp_sk(left) != idemp_sk(right)


@pytest.mark.parametrize("key", [
    "invoice/a", "run:1", "job#7", "a" * 4000, "", "  spaces  ", "日本語のキー",
])
def test_the_sort_key_stays_inside_the_namespace_and_the_size_limit(key):
    """The two things the sanitising version existed for, kept: a `#` in the key
    cannot forge another sk namespace, and no key can approach the 2KB sort-key
    limit."""
    sk = idemp_sk(key)
    assert sk.startswith("EV#IDEMP#")
    assert "#" not in sk[len("EV#IDEMP#"):]
    assert len(sk.encode("utf-8")) < 128


def test_the_token_is_a_function_of_the_whole_key():
    """Truncation is what made two long keys with a shared prefix collide."""
    long_a = "x" * 600 + "a"
    long_b = "x" * 600 + "b"
    assert _legacy_safe_idemp_token(long_a) == _legacy_safe_idemp_token(long_b)
    assert _idemp_token(long_a) != _idemp_token(long_b)


def test_a_key_that_needed_no_sanitising_still_moves_to_the_digest():
    """Otherwise the change would be a no-op for exactly the keys most clients send,
    and the legacy fallback below would never be exercised in production."""
    plain = "invoice-2026-08-30"
    assert legacy_idemp_sk(plain) == f"EV#IDEMP#{plain}"
    assert idemp_sk(plain) != legacy_idemp_sk(plain)


# ---------------------------------------------------------------------------
# the read fallback, so a key from before the change is not authorized twice
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(dynamodb_mock):
    """The repository against the ledger table `dynamodb_mock` already creates."""
    from dynamo.credit_ledger import CreditLedgerRepository

    return CreditLedgerRepository()


def test_a_row_written_before_the_digest_is_still_found(ledger, dynamodb_mock):
    from dynamo.credit_ledger import ledger_pk

    key = "invoice/a"
    dynamodb_mock.Table("stratoclave-credit-ledger").put_item(Item={
        "pk": ledger_pk("acme", "2026-08"),
        "sk": legacy_idemp_sk(key),
        "idempotency_key": key,
        "authorization_id": "auth-legacy",
        "amount_microusd": 1_000,
    })
    row = ledger.get_idemp(tenant_id="acme", period="2026-08", idempotency_key=key)
    assert row is not None, "a retry of a pre-existing key would mint a second hold"
    assert row["authorization_id"] == "auth-legacy"


def test_the_digest_row_wins_when_both_exist(ledger, dynamodb_mock):
    """A key used before and after the change: the new row is the current record."""
    from dynamo.credit_ledger import ledger_pk

    key = "invoice/a"
    table = dynamodb_mock.Table("stratoclave-credit-ledger")
    for sk, auth in ((legacy_idemp_sk(key), "auth-legacy"), (idemp_sk(key), "auth-new")):
        table.put_item(Item={
            "pk": ledger_pk("acme", "2026-08"), "sk": sk,
            "idempotency_key": key, "authorization_id": auth, "amount_microusd": 1_000,
        })
    row = ledger.get_idemp(tenant_id="acme", period="2026-08", idempotency_key=key)
    assert row["authorization_id"] == "auth-new"


def test_an_unused_key_reads_as_absent(ledger):
    assert ledger.get_idemp(
        tenant_id="acme", period="2026-08", idempotency_key="never-used"
    ) is None
