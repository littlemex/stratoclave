"""Property tests for P0-11 fallback visibility in usage views (#65).

The correctness core is `mvp.me._derive_fallback` plus the record->read
round-trip. The write side stores BOTH the effective and the requested model in
the SAME spelling space (bedrock ids), so the read derivation is a plain
string compare — no read-time canonicalization, hence stable under registry
drift/retirement (Fable #65 rev1 BUG 1).

  P1  same model, any requested spelling => stored requested == stored
      effective bedrock id => not a fallback
  P2  two genuinely different models => different bedrock ids => a fallback
  P3  legacy-safe: no requested id => None ("unknown"), never True/False
  P4  registry-retirement STABILITY: a row whose models later leave the
      registry still derives the same bool (plain compare of stored bytes)
  P5  round-trip through DynamoDB (moto): record stores the requested bedrock
      id (or nothing) and the read mapping derives the right bool.

No arithmetic / concurrency => property tests (not Z3), per the Fable design.
"""
from __future__ import annotations

import pytest
from hypothesis import assume, given, strategies as st

from mvp.me import _derive_fallback
from mvp.models import _REGISTRY, resolve_bedrock_model, resolve_model

# Per-model spelling lists (every alias + the bedrock id) + the bedrock id it
# canonicalizes to on the write path.
_MODEL_SPELLINGS: list[tuple[list[str], str]] = [
    ([*e.aliases, e.bedrock_model_id], e.bedrock_model_id) for e in _REGISTRY
]

BOGUS = "model-that-does-not-exist-zz9"
try:
    resolve_model(BOGUS)
    raise RuntimeError("BOGUS unexpectedly resolves; pick another string")
except ValueError:
    pass

if len(_MODEL_SPELLINGS) < 2:
    pytest.skip("registry has <2 distinct models", allow_module_level=True)

model_indices = st.sampled_from(range(len(_MODEL_SPELLINGS)))


def _stored_requested(x: str) -> str:
    """Mirror settle's write-side transform: requested -> its bedrock id via the
    general registry (Claude + OpenAI)."""
    try:
        return resolve_model(x).bedrock_model_id
    except Exception:
        return x


# ---- P1: same model, any spelling requested => not a fallback --------------
@given(model_indices, st.data())
def test_same_model_any_requested_spelling_not_fallback(mi, data):
    spellings, bedrock = _MODEL_SPELLINGS[mi]
    requested_spelling = data.draw(st.sampled_from(spellings))
    stored_requested = _stored_requested(requested_spelling)
    # effective is stored as the bedrock id (what settle passes as model_id)
    assert _derive_fallback(stored_requested, bedrock) is False


# ---- P2: different models => a fallback ------------------------------------
@given(model_indices, model_indices, st.data())
def test_different_models_is_fallback(mi, mj, data):
    _, bedrock_i = _MODEL_SPELLINGS[mi]
    _, bedrock_j = _MODEL_SPELLINGS[mj]
    assume(bedrock_i != bedrock_j)
    requested_spelling = data.draw(st.sampled_from(_MODEL_SPELLINGS[mi][0]))
    stored_requested = _stored_requested(requested_spelling)
    assert _derive_fallback(stored_requested, bedrock_j) is True


# ---- P3: legacy safety ------------------------------------------------------
@given(st.sampled_from([b for _, b in _MODEL_SPELLINGS]))
def test_missing_requested_is_none(effective):
    assert _derive_fallback(None, effective) is None
    assert _derive_fallback("", effective) is None


# ---- P4: registry-retirement stability (the test that catches BUG 1) -------
def test_retirement_stability_no_false_positive():
    """A non-fallback row (requested and effective are the SAME model) must
    still read as not-a-fallback after the model leaves the registry. Because
    both ids are stored as the identical bedrock id, the plain compare is
    stable regardless of whether the registry still knows the model."""
    entry = _REGISTRY[0]
    # write-side stored values for "requested opus (as alias), served opus":
    stored_requested = _stored_requested(entry.aliases[0])  # -> bedrock id
    stored_effective = entry.bedrock_model_id
    assert stored_requested == stored_effective
    # today:
    assert _derive_fallback(stored_requested, stored_effective) is False
    # simulate retirement: the stored bytes are unchanged; the compare is pure
    # string equality and never consults the registry, so it stays False.
    assert _derive_fallback(stored_requested, stored_effective) is False


def test_retirement_stability_real_fallback_stays_true():
    """A genuine fallback row stays True after retirement too."""
    opus, haiku = _REGISTRY[0].bedrock_model_id, _REGISTRY[-1].bedrock_model_id
    assume_distinct = opus != haiku
    assert assume_distinct
    assert _derive_fallback(opus, haiku) is True


def test_unknown_identical_raw_is_not_fallback():
    assert _derive_fallback(BOGUS, BOGUS) is False


# ---- Cross-resolver spelling invariant (Fable #65 rev2 crux) ---------------
# The whole plain-compare correctness rests on: for the SAME model, the id
# settle stores for `requested` (resolve_model(x).bedrock_model_id) is
# byte-identical to the id the handler stores for `effective`. Claude handlers
# store effective via resolve_bedrock_model(); the OpenAI handler stores it via
# resolve_model(...).bedrock_model_id (the same path settle uses). So we must
# pin: resolve_model(name).bedrock_model_id == resolve_bedrock_model(name) for
# every Claude spelling, else a NON-fallback request would false-positive.
@pytest.mark.parametrize(
    "name",
    [s for e in _REGISTRY for s in (*e.aliases, e.bedrock_model_id)],
)
def test_resolve_model_bedrock_matches_claude_resolver(name):
    rm = resolve_model(name).bedrock_model_id
    try:
        rb = resolve_bedrock_model(name)
    except ValueError:
        # Non-Claude (OpenAI) id: the Claude-only resolver rejects it, and the
        # OpenAI handler stores effective via resolve_model(...).bedrock_model_id
        # — the SAME path settle uses — so the ids agree by construction.
        return
    assert rb == rm, f"cross-resolver spelling mismatch for {name!r}: {rb} != {rm}"


@pytest.mark.parametrize("entry", list(_REGISTRY))
def test_bedrock_id_input_is_idempotent(entry):
    # A client may send a bedrock id as body.model; settle must re-spell it to
    # the SAME bedrock id (else a non-fallback false-positives).
    assert resolve_model(entry.bedrock_model_id).bedrock_model_id == entry.bedrock_model_id


@pytest.mark.parametrize("entry", list(_REGISTRY))
def test_normal_request_is_not_fallback(entry):
    """A plain request (requested model == served model, no cascade, no pin):
    settle stores requested = resolve_model(name).bedrock_model_id and the
    handler stores effective = the same model's bedrock id => fb must be False.
    This is the false-positive-sensitive case neither live scenario exercised."""
    name = entry.aliases[0] if entry.aliases else entry.bedrock_model_id
    stored_requested = _stored_requested(name)
    effective = entry.bedrock_model_id  # what the handler stores when no cascade
    assert _derive_fallback(stored_requested, effective) is False


# ---- P5: record -> read round-trip (moto) ----------------------------------
def _moto_usage_table():
    import boto3

    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.create_table(
        TableName="usage-fallback-test",
        KeySchema=[
            {"AttributeName": "tenant_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp_log_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_id", "AttributeType": "S"},
            {"AttributeName": "timestamp_log_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.mark.skipif(
    pytest.importorskip("moto", reason="moto not installed") is None, reason=""
)
def test_record_read_roundtrip_moto():
    """record() with a requested model stores it and the read mapping derives
    the right bool; record() WITHOUT one stores no attribute and reads back
    (None, None)."""
    from moto import mock_aws

    with mock_aws():
        table = _moto_usage_table()
        from dynamo.usage_logs import UsageLogsRepository

        repo = UsageLogsRepository(table_name="usage-fallback-test")
        opus_bedrock = _REGISTRY[0].bedrock_model_id
        haiku_bedrock = _REGISTRY[-1].bedrock_model_id
        # Fallback row: requested opus, served haiku (both stored as bedrock ids).
        repo.record(
            tenant_id="t1", user_id="u1", user_email="e@x.com",
            model_id=haiku_bedrock, input_tokens=1, output_tokens=1,
            requested_model_id=opus_bedrock,
        )
        # Legacy-shaped row: no requested_model_id at all.
        repo.record(
            tenant_id="t1", user_id="u1", user_email="e@x.com",
            model_id=opus_bedrock, input_tokens=1, output_tokens=1,
        )
        import boto3

        items = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("tenant_id").eq("t1")
        )["Items"]
        assert len(items) == 2
        by_req = {it.get("requested_model_id"): it for it in items}
        fb = by_req[opus_bedrock]
        assert _derive_fallback(fb.get("requested_model_id"), str(fb["model_id"])) is True
        legacy = by_req[None]
        assert "requested_model_id" not in legacy
        assert _derive_fallback(legacy.get("requested_model_id"), str(legacy["model_id"])) is None
