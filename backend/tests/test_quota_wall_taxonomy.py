"""F2 (docs/design/quota-raises.md): R27 + R37, and seam amendment B4 (SEAMS S7,
S12) — the FINAL `raise_hint` envelope, shipped now with degenerate F2
content so F3 fills it with no renames and no removals.

docs/design/quota-raises.md's design: `_err_402`/`_err_402_does_not_fit` gain a REQUIRED
keyword-only `wall` argument; `mvp.reserve_limits.LimitKind` gains a
`grantable: bool` field (True only for `tenant_dollar_pool`); `_err_402`
looks `wall` up via `mvp.reserve_limits.is_grantable_wall`.

  R27 — `personal_budget_exhausted` (wall="user_token_quota", not grantable)
        must never carry `raise_hint`.
  R37 — EVERY refusal names its wall and its grantability, including
        `model_quota_exhausted` (wall="per_model_quota") — which is money,
        but not grantable, regardless of whether the tenant- or user-scoped
        counter is what actually tripped.

B4 (final schema, F2 owns it): when grantable, `raise_hint` is no longer a
bare `True` — it is the envelope F3 will later populate with more
candidates and real pricing:

    "raise_hint": {
      "candidates": [
        {"wall": "tenant_dollar_pool", "model": None, "shortfall_microusd": None}
      ],
      "remaining_cap_microusd": <int>,   # S12: the hard ceiling on what ANY
                                          # approver may grant, right now
    }

Under F2 the candidate list holds EXACTLY ONE element (the refusing wall
itself — F2 has no cascade-pricing data; see docs/design/quota-raises.md's note on S11),
with `model`/`shortfall_microusd` left `None` (pricing fields optional, per
B4). `_err_402` therefore needs `pool_granted_microusd` and
`effective_cap_microusd` (B1: `grant_cap_microusd` if present, else the live
baseline) to compute `remaining_cap_microusd = max(0, effective_cap -
pool_granted)` — both REQUIRED together whenever `wall` is grantable, so a
call site cannot ship a grantable refusal with no cap information (fail
loudly, matching this module's own convention elsewhere).

A static check closes the loophole a purely-functional test of `_err_402`
alone would leave open: every CALL SITE in `mvp/_pipeline.py` must actually
pass `wall=`, or the classification is unreachable in practice even once
`_err_402` itself supports it.
"""
from __future__ import annotations

import ast
import inspect

import pytest


def _err_402_call_sites() -> list[ast.Call]:
    import mvp._pipeline as pipeline_module

    tree = ast.parse(inspect.getsource(pipeline_module))
    calls = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            if isinstance(node.func, ast.Name) and node.func.id in (
                "_err_402", "_err_402_does_not_fit",
            ):
                calls.append(node)
            self.generic_visit(node)

    Visitor().visit(tree)
    return calls


def test_r37_every_err_402_call_site_in_pipeline_names_its_wall():
    """Static/shape check (CONTRACTS.md's own 'E' category: a violation of
    shape, not a value). Fails today because zero call sites pass `wall=` —
    the keyword does not exist yet."""
    calls = _err_402_call_sites()
    assert calls, "expected to find _err_402 call sites in mvp/_pipeline.py"
    missing = [
        ast.dump(c) for c in calls
        if not any(kw.arg == "wall" for kw in c.keywords)
    ]
    assert not missing, (
        f"{len(missing)} of {len(calls)} _err_402/_err_402_does_not_fit call "
        "sites in mvp/_pipeline.py do not pass wall=; a refusal that cannot "
        "name its wall cannot satisfy R37"
    )


def test_r37_reserve_limits_registry_declares_grantability():
    from mvp.reserve_limits import RESERVE_LIMITS

    by_name = {k.name: k for k in RESERVE_LIMITS}
    assert by_name["tenant_dollar_pool"].grantable is True
    assert by_name["user_token_quota"].grantable is False
    assert by_name["per_model_quota"].grantable is False


def test_r37_is_grantable_wall_helper():
    from mvp.reserve_limits import is_grantable_wall

    assert is_grantable_wall("tenant_dollar_pool") is True
    assert is_grantable_wall("user_token_quota") is False
    assert is_grantable_wall("per_model_quota") is False


def test_r27_personal_budget_exhausted_carries_no_raise_hint():
    from mvp._pipeline import _err_402

    exc = _err_402("personal_budget_exhausted", wall="user_token_quota")
    assert exc.status_code == 402
    assert "raise_hint" not in exc.detail
    assert exc.detail["wall"] == "user_token_quota"
    assert exc.detail["grantable"] is False


def test_b4_tenant_pool_exhausted_carries_the_final_envelope_with_one_degenerate_candidate():
    """B4: the envelope F3 will later fill, shipped now with exactly one
    candidate (the refusing wall, F2 has no cascade-pricing data) and
    optional pricing fields left None — not a bare `True`.

    The implementation's `_err_402` takes `pool_row=` (the pool row the
    refusal already read), not the two primitives
    `pool_granted_microusd=`/`effective_cap_microusd=` docs/design/quota-raises.md drafted:
    `mvp.grants.raise_hint_for_pool_row` derives both from the SAME row via
    `granted_microusd`/`effective_grant_cap_for_row`, so a call site that
    already has the row open (every real one does — see
    `mvp/_pipeline.py::_refusal_body`'s own docstring) passes it once rather
    than computing two figures from it itself."""
    from mvp._pipeline import _err_402

    pool_row = {"pool_granted_microusd": 3_000_000, "grant_cap_microusd": 10_000_000}
    exc = _err_402(
        "tenant_pool_exhausted", wall="tenant_dollar_pool", pool_row=pool_row,
    )
    assert exc.detail["wall"] == "tenant_dollar_pool"
    assert exc.detail["grantable"] is True
    hint = exc.detail["raise_hint"]
    assert hint["candidates"] == [
        {"blocker": "tenant_pool", "wall": "tenant_dollar_pool",
         "model": None, "shortfall_microusd": None}
    ]
    assert hint["remaining_cap_microusd"] == 7_000_000


def test_b4_envelope_remaining_cap_floors_at_zero_when_already_over_cap():
    """A pool already at or past its effective cap (e.g. the cap was lowered
    after grants were already outstanding) must report a non-negative
    remaining figure — never a number that would tell a console it can
    request MORE room than none at all."""
    from mvp._pipeline import _err_402

    pool_row = {"pool_granted_microusd": 12_000_000, "grant_cap_microusd": 10_000_000}
    exc = _err_402(
        "tenant_pool_exhausted", wall="tenant_dollar_pool", pool_row=pool_row,
    )
    assert exc.detail["raise_hint"]["remaining_cap_microusd"] == 0


def test_b4_grantable_wall_with_no_pool_row_omits_the_hint_rather_than_raising():
    """A grantable refusal built with NO pool row in hand omits the hint
    rather than raising: `_refusal_body`'s own docstring states the
    reasoning directly — 'a raised exception here would turn a money
    refusal into a 500 on the path least able to afford one'. Several real
    call sites (`mvp/_pipeline.py`'s best-effort `_hint_row` re-reads) can
    legitimately have no row at the moment of refusal, e.g. when the
    best-effort re-read itself fails; a hard failure there is strictly
    worse than a refusal with a poorer hint. (docs/design/quota-raises.md's draft wanted a
    `ValueError` here, treating `None` as always a caller bug — but real
    call sites show `None` is a genuine, anticipated runtime state, not
    only a programming error, so failing loudly on it would be wrong.)"""
    from mvp._pipeline import _err_402

    exc = _err_402("tenant_pool_exhausted", wall="tenant_dollar_pool", pool_row=None)
    assert exc.detail["grantable"] is True
    assert "raise_hint" not in exc.detail


def test_r37_model_quota_exhausted_is_money_but_not_grantable_either_scope():
    """R37's own emphasis: being denominated in micro-USD does not make the
    per-model quota's USER scope grantable. The wall identity is
    `per_model_quota` regardless of which scope (tenant or user) actually
    tripped it — F2 does not split it into a fourth wall."""
    from mvp._pipeline import _err_402

    exc = _err_402("model_quota_exhausted", wall="per_model_quota")
    assert exc.detail["wall"] == "per_model_quota"
    assert exc.detail["grantable"] is False
    assert "raise_hint" not in exc.detail


def test_r37_err_402_refuses_to_classify_an_undeclared_wall():
    """A `wall` name absent from the registry must fail loudly (fail-closed,
    matching this codebase's own convention in mvp/authz.py's implication
    lattice guard), not silently default to non-grantable."""
    from mvp._pipeline import _err_402

    with pytest.raises((ValueError, KeyError)):
        _err_402("something_exhausted", wall="not_a_declared_wall")


def test_r27_wall_is_a_required_argument():
    """`wall` must be REQUIRED, not defaulted — a call site that forgets it
    must fail at call time (TypeError), not silently ship a body with no
    wall/grantable classification at all."""
    from mvp._pipeline import _err_402

    with pytest.raises(TypeError):
        _err_402("personal_budget_exhausted")  # no wall= supplied


# ---------------------------------------------------------------------------
# U1 — `mvp/grants.py`'s exact public surface: `RaiseHint` is a named model,
# not a dict `_err_402` invents inline, and `is_capacity_bearing` is DEFINED
# in `mvp.grants` — not re-exported from the `dynamo` layer, which does not
# carry it at all (a follow-up correction: three independent consumers of a
# single-source-of-fact predicate must import ONE name, not two paths to the
# same object — see `test_u1_is_capacity_bearing_is_defined_in_mvp_grants_and_nowhere_else`).
# ---------------------------------------------------------------------------

def test_u1_raise_hint_is_importable_and_shaped_per_b4():
    """`RaiseHintCandidate` carries a fourth field, `blocker` (required) —
    the wall's PUBLIC name (`mvp.grants.blocker_for_wall`), an addition
    beyond docs/design/quota-raises.md's three-field draft. `RaiseHint` also carries
    `reason_codes` (defaulted from `RAISE_REASON_CODES`), so a console
    rendering the hint does not need a second request to learn what reasons
    a raise accepts. Both are additive — dumped alongside, not replacing,
    the fields docs/design/quota-raises.md pinned."""
    from mvp.grants import RAISE_REASON_CODES, RaiseHint, RaiseHintCandidate

    hint = RaiseHint(
        candidates=[RaiseHintCandidate(blocker="tenant_pool", wall="tenant_dollar_pool")],
        remaining_cap_microusd=7_000_000,
    )
    dumped = hint.model_dump()
    assert dumped == {
        "candidates": [{"blocker": "tenant_pool", "wall": "tenant_dollar_pool",
                         "model": None, "shortfall_microusd": None}],
        "remaining_cap_microusd": 7_000_000,
        "reason_codes": list(RAISE_REASON_CODES),
    }


def test_u1_err_402_raise_hint_body_is_exactly_a_raisehint_dump():
    """The dict `_err_402` puts under `raise_hint` must be byte-identical to
    what constructing a `RaiseHint` and dumping it produces — the model is
    the shape, not a second, parallel one `_err_402` maintains by hand."""
    from mvp._pipeline import _err_402
    from mvp.grants import RaiseHint, RaiseHintCandidate

    pool_row = {"pool_granted_microusd": 3_000_000, "grant_cap_microusd": 10_000_000}
    exc = _err_402(
        "tenant_pool_exhausted", wall="tenant_dollar_pool", pool_row=pool_row,
    )
    expected = RaiseHint(
        candidates=[RaiseHintCandidate(blocker="tenant_pool", wall="tenant_dollar_pool")],
        remaining_cap_microusd=7_000_000,
    ).model_dump()
    assert exc.detail["raise_hint"] == expected


def test_u1_is_capacity_bearing_is_defined_in_mvp_grants_and_nowhere_else():
    """Corrected conclusion (this test originally asserted the OPPOSITE — a
    re-export from `dynamo.quota_events` — and that was the wrong side: the
    question `is_capacity_bearing` answers ("does this grant currently
    contribute to pool_granted") is a lifecycle rule that happens to read
    stored fields, not a fact about storage, and all three consumers (F2's
    own grant lifecycle code, F1's reconciler, F3's inventory) live in the
    `mvp` layer. A re-export is fine for a function whose behaviour is
    settled and whose location is incidental; it is wrong for anything that
    exists to BE the single source of a fact — three independent consumers
    must import ONE name, never two paths to the same object, or the day
    someone changes the predicate they change one path and not the other.
    So: `mvp.grants.is_capacity_bearing` must exist, must be DEFINED there
    (`__module__ == "mvp.grants"`, not imported from elsewhere under an
    alias), and `dynamo.quota_events` must carry no such attribute at all."""
    from mvp.grants import is_capacity_bearing

    assert is_capacity_bearing.__module__ == "mvp.grants", (
        "must be DEFINED in mvp.grants, not imported/aliased from another module"
    )
    assert is_capacity_bearing("ACTIVE") is True
    assert is_capacity_bearing("EXPIRED") is False

    import dynamo.quota_events as quota_events_module

    assert not hasattr(quota_events_module, "is_capacity_bearing"), (
        "the storage layer must not carry this lifecycle predicate at all — "
        "not as a re-export, not as a module-level alias"
    )
    assert not hasattr(quota_events_module.QuotaEventsRepository, "is_capacity_bearing"), (
        "nor on the repository class itself — a predicate three independent "
        "consumers must never restate needs exactly one name, defined once"
    )
