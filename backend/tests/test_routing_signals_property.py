"""P0-16 property tests — routing_signals key scheme + fire-and-forget guarantees.

Formal-strategy decision (P0-16): NO Z3. The signal is one unconditional
PutItem riding the P0-13/14 finalizer claim (already proven at-most-once);
there is no counter arithmetic and no new interleaving. The real risks are
(a) the day-bucket/shard key function (uniform, key-safe, deterministic,
time-ordered sk) and (b) "never raises / never blocks the request".
Hypothesis covers (a)'s logical properties; a seeded statistical test covers
shard uniformity (Hypothesis is the wrong tool for distributions); example
tests cover (b).
"""
from __future__ import annotations

import random
import re
import string

import pytest
from hypothesis import given, settings, strategies as st

from mvp.learning import signals

# Mirrors observability.store's key grammar (single source of truth is
# _safe_key_token, which signals reuses; this regex is the test oracle).
TOKEN_RE = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

any_text = st.text(min_size=0, max_size=300)  # includes '#', NUL, emoji, 300-char ids
any_ms = st.integers(min_value=0, max_value=9_999_999_999_999)  # 13-digit ceiling (~year 2286)


def _full_kwargs(**overrides):
    kw = dict(
        tenant_id="org_123", group_id="g1", workflow_run_id="wr1",
        span_id="sp_abc", category="sonnet",
        committed_model_id="anthropic.claude-3-5-sonnet", committed_region="us-east-1",
        cost_tier=2, chain_position_served=0, status="success",
        usage_is_partial=False, canceled_by_client=False, output_tokens=42,
        latency_first_event_ms=120, attempts_total=1, targets_distinct=1,
        breaker_stage="closed",
    )
    kw.update(overrides)
    return kw


# ---------------------------------------------------------------- shard_for

@given(any_text, st.integers(min_value=1, max_value=64))
def test_shard_deterministic_and_in_range(span_id, shards):
    a = signals.shard_for(span_id, shards)
    assert a == signals.shard_for(span_id, shards)
    assert 0 <= a < shards


def test_shard_uniformity_over_uuid_shaped_span_ids():
    """Seeded (deterministic) distribution check: crc32 over uuid-shaped ids
    must be near-uniform across 8 shards. Bound is loose (±20% of expected)
    so this never flakes; real crc32 skew at n=20k is <5%."""
    shards, n = 8, 20_000
    rnd = random.Random(0x50_16)  # P0-16, fixed seed
    counts = [0] * shards
    for _ in range(n):
        counts[signals.shard_for(f"{rnd.getrandbits(128):032x}", shards)] += 1
    expected = n / shards
    assert min(counts) > expected * 0.8, counts
    assert max(counts) < expected * 1.2, counts


# ---------------------------------------------------------------- key grammar

@given(any_text, any_text, any_text, any_ms)
def test_keys_are_grammar_clean_and_bounded(tenant, category, span_id, ts):
    pk, sk = signals.build_keys(
        tenant_id=tenant, category=category, span_id=span_id, created_at_ms=ts
    )
    # pk: TENANT#<tok>#CAT#<tok>#D#<yyyymmdd>#S#<shard> — a hostile '#' in any
    # input must NOT change the part count (it gets hashed away).
    p = pk.split("#")
    assert len(p) == 8
    assert p[0] == "TENANT" and p[2] == "CAT" and p[4] == "D" and p[6] == "S"
    assert TOKEN_RE.match(p[1]) and TOKEN_RE.match(p[3])
    assert re.fullmatch(r"\d{8}", p[5])
    assert 0 <= int(p[7]) < 64
    # sk: TS#<013d>#<tok>
    s = sk.split("#")
    assert len(s) == 3 and s[0] == "TS"
    assert re.fullmatch(r"\d{13}", s[1])
    assert TOKEN_RE.match(s[2])
    # DynamoDB hard limits: pk <= 2048B, sk <= 1024B
    assert len(pk.encode("utf-8")) <= 2048
    assert len(sk.encode("utf-8")) <= 1024


@given(any_ms, any_ms, any_text)
def test_sk_lexicographic_order_matches_time_order(t1, t2, span_id):
    """The 013d zero-pad is exactly why this holds; a regression to %d breaks
    it at the 10^12→10^13 boundary."""
    _, sk1 = signals.build_keys(tenant_id="t", category="c", span_id=span_id, created_at_ms=t1)
    _, sk2 = signals.build_keys(tenant_id="t", category="c", span_id=span_id, created_at_ms=t2)
    assert (t1 < t2) == (sk1 < sk2)
    assert (t1 == t2) == (sk1 == sk2)


@given(any_text, any_text)
def test_shard_is_a_function_of_span_id_only(span_id, other):
    """pk shard must be recomputable from the sk's span token alone (the future
    evaluator relies on this to locate an item's shard)."""
    pk1, sk1 = signals.build_keys(tenant_id="t1", category=other, span_id=span_id, created_at_ms=0)
    pk2, _ = signals.build_keys(tenant_id="t2", category="c", span_id=span_id, created_at_ms=999)
    assert pk1.rsplit("#", 1)[1] == pk2.rsplit("#", 1)[1]
    # and it equals shard_for(<safe token in the sk>)
    tok = sk1.split("#")[2]
    assert int(pk1.rsplit("#", 1)[1]) == signals.shard_for(tok, signals._SHARDS)


# ------------------------------------------------------------ never raises

def test_emit_signal_sync_never_raises_when_dynamo_is_down(monkeypatch):
    import dynamo.client as dc

    def boom(*a, **k):
        raise RuntimeError("dynamo unreachable")

    monkeypatch.setattr(dc, "get_dynamodb_resource", boom)
    signals.emit_signal_sync(**_full_kwargs())  # must swallow


def test_emit_signal_never_raises_when_executor_is_dead(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("executor shut down")

    monkeypatch.setattr(signals._executor, "submit", boom)
    signals.emit_signal(**_full_kwargs())  # must swallow (and release its slot)
    # slot was released: a subsequent emit can still acquire
    assert signals._slots.acquire(blocking=False)
    signals._slots.release()


def test_emit_signal_drops_silently_when_queue_full(monkeypatch):
    class NeverSem:
        def acquire(self, blocking=False):
            return False
    monkeypatch.setattr(signals, "_slots", NeverSem())
    signals.emit_signal(**_full_kwargs())  # drop, no raise, no block


def test_written_item_matches_key_scheme(monkeypatch):
    captured = {}

    class FakeTable:
        def put_item(self, Item):
            captured.update(Item)

    class FakeResource:
        def Table(self, name):
            return FakeTable()

    import dynamo.client as dc
    monkeypatch.setattr(dc, "get_dynamodb_resource", lambda: FakeResource())
    signals.emit_signal_sync(**_full_kwargs(span_id="sp weird#id", canceled_by_client=True))
    pk, sk = signals.build_keys(
        tenant_id="org_123", category="sonnet", span_id="sp weird#id",
        created_at_ms=captured["created_at_ms"],
    )
    assert captured["pk"] == pk and captured["sk"] == sk
    assert captured["canceled_by_client"] is True
    assert captured["status"] == "success"
    # TTL: epoch SECONDS, ~90d out, defensively bounded
    assert captured["expires_at"] > captured["created_at_ms"] // 1000 + 86_400
    assert captured["expires_at"] <= captured["created_at_ms"] // 1000 + 3650 * 86_400


# ------------------------------------------------------------ env hardening

@pytest.mark.parametrize("raw,expected", [
    ("banana", 8), ("", 8), ("0", 1), ("-3", 1), ("9999", 64), ("16", 16),
])
def test_shards_env_parse_is_defensive(raw, expected):
    assert signals._env_int_from(raw, default=8, lo=1, hi=64) == expected


# ------------------------------------------------------------ category (F1)

def test_category_alias_wins_over_committed_model():
    """Fable rev1 F1: a cross-tier fallback whose alias says one tier but whose
    committed model is another must bucket by the ALIAS (category is a pk dim)."""
    # alias sonnet, committed haiku -> sonnet (alias checked fully first)
    assert signals.category_for_model("team-sonnet-default",
                                      "anthropic.claude-3-5-haiku-x") == "sonnet"
    # alias empty -> fall through to the committed model id
    assert signals.category_for_model("", "anthropic.claude-3-opus") == "opus"
    assert signals.category_for_model("gpt-x", "some-model") == "other"


def test_written_item_carries_shard_count(monkeypatch):
    """Fable rev1 F7: the item records the write-time shard count so a future
    consumer can enumerate S#0..N-1 even if SC_SIGNAL_SHARDS later changes."""
    captured = {}

    class FakeTable:
        def put_item(self, Item):
            captured.update(Item)

    class FakeResource:
        def Table(self, name):
            return FakeTable()

    import dynamo.client as dc
    monkeypatch.setattr(dc, "get_dynamodb_resource", lambda: FakeResource())
    signals.emit_signal_sync(**_full_kwargs())
    assert captured["shards"] == signals._SHARDS
