"""Tests for the auto-issued Savings Certificate store (mvp.learning.certificate_store).

Proves the honesty guards are load-bearing: data-absent is a documented skip not a
$0 forgery, low reconcile coverage is not stamped final, the store is write-once
(re-run = no-op, never overwrite), and it refuses synthetic provenance or a
certificate that dropped its honesty caveats.
"""
from __future__ import annotations

import pytest

from mvp.learning import certificate_store as cs


# ---------------------------------------------------------------- fixtures

def _savings_block(*, potential_note=True, quality_measured=False, enacted=False):
    savings = {
        "net_saving_microusd": 1_000,
        "potential": {
            "net_saving_microusd": 5_000,
            "enacted": enacted,
        },
        "quality": {"measured": quality_measured},
    }
    if potential_note:
        savings["potential"]["note"] = "UPPER-BOUND estimate; quality unmeasurable."
    return savings


def _cert(*, traffic="real", vsr_acted=10, unsettled=0, **savings_kw):
    return {
        "tenant_id": "acme", "day": "20260717", "traffic": traffic,
        "rate_version": "builtin-defaults",
        "savings": _savings_block(**savings_kw),
        "reconcile": {"vsr_acted_count": vsr_acted, "unsettled_count": unsettled,
                      "matched_count": vsr_acted - unsettled},
    }


def _fake_certificate_fn(cert):
    def _fn(*, tenant_id, day, traffic):
        c = dict(cert)
        c["tenant_id"], c["day"], c["traffic"] = tenant_id, day, traffic
        return c
    return _fn


# ---------------------------------------------------------------- issue logic

def test_issue_skips_empty_day_not_zero_certificate():
    out = cs.issue_certificate(tenant_id="acme", day="20260717", generated_at_ms=1,
                               certificate_fn=_fake_certificate_fn(_cert(vsr_acted=0)))
    assert out.issued is False and out.skip_reason == cs.SKIP_NO_TRAFFIC
    assert out.certificate is None            # never a $0 forgery


def test_issue_skips_when_reconcile_coverage_too_low():
    # 5/10 unsettled = 50% unmatched > 10% -> not final-certifiable.
    out = cs.issue_certificate(tenant_id="acme", day="20260717", generated_at_ms=1,
                               certificate_fn=_fake_certificate_fn(
                                   _cert(vsr_acted=10, unsettled=5)))
    assert out.issued is False and out.skip_reason == cs.SKIP_UNMATCHED_HIGH


def test_issue_produces_envelope_on_good_day():
    out = cs.issue_certificate(tenant_id="acme", day="20260717", generated_at_ms=12345,
                               certificate_fn=_fake_certificate_fn(_cert()))
    assert out.issued is True
    env = out.certificate
    assert env["schema_version"] == cs.CERTIFICATE_SCHEMA_VERSION
    assert env["generated_at_ms"] == 12345 and env["status"] == "final"
    assert env["certificate"]["traffic"] == "real"


# ---------------------------------------------------------------- persistence (moto)

def _good_envelope(day="20260717", revision=0):
    return {
        "record_type": "savings_certificate",
        "schema_version": cs.CERTIFICATE_SCHEMA_VERSION,
        "tenant_id": "acme", "day": day, "generated_at_ms": 1,
        "status": "final", "revision": revision,
        "certificate": _cert(),
    }


def test_store_is_write_once(dynamodb_mock):
    assert cs.store_certificate(_good_envelope()) is True        # fresh write
    # a re-run with the SAME (tenant, day, revision) is an idempotent no-op.
    assert cs.store_certificate(_good_envelope()) is False
    got = cs.get_certificate(tenant_id="acme", day="20260717")
    assert got is not None and got["schema_version"] == cs.CERTIFICATE_SCHEMA_VERSION


def test_new_revision_is_a_separate_row(dynamodb_mock):
    assert cs.store_certificate(_good_envelope(revision=0)) is True
    assert cs.store_certificate(_good_envelope(revision=1)) is True   # supersedes, not overwrite
    assert cs.get_certificate(tenant_id="acme", day="20260717", revision=0) is not None
    assert cs.get_certificate(tenant_id="acme", day="20260717", revision=1) is not None


def test_get_latest_returns_highest_revision(dynamodb_mock):
    assert cs.store_certificate(_good_envelope(revision=0)) is True
    assert cs.store_certificate(_good_envelope(revision=1)) is True
    latest = cs.get_latest_certificate(tenant_id="acme", day="20260717")
    assert latest is not None and latest["revision"] == 1


def test_issue_revision_carries_self_describing_supersedes():
    out = cs.issue_certificate(tenant_id="acme", day="20260717", generated_at_ms=200,
                               revision=1, supersedes_generated_at_ms=100,
                               supersede_reason="late_settle",
                               certificate_fn=_fake_certificate_fn(_cert()))
    assert out.issued is True
    sup = out.certificate["supersedes"]
    assert sup["revision"] == 0 and sup["generated_at_ms"] == 100
    assert sup["reason"] == "late_settle"


def test_issue_rejects_nonpositive_generated_at():
    with pytest.raises(ValueError, match="generated_at_ms must be positive"):
        cs.issue_certificate(tenant_id="acme", day="20260717", generated_at_ms=0,
                             certificate_fn=_fake_certificate_fn(_cert()))


def test_issue_and_store_collision_returns_stored_not_recompute(dynamodb_mock):
    # first issue with one savings figure.
    first = _cert()
    first["savings"]["net_saving_microusd"] = 1_000
    out1 = cs.issue_and_store(tenant_id="acme", day="20260717", generated_at_ms=1,
                              certificate_fn=_fake_certificate_fn(first))
    assert out1.issued is True and out1.already_existed is False
    # second issue (same day/revision) with a DIFFERENT recompute -> collision.
    second = _cert()
    second["savings"]["net_saving_microusd"] = 999_999
    out2 = cs.issue_and_store(tenant_id="acme", day="20260717", generated_at_ms=2,
                              certificate_fn=_fake_certificate_fn(second))
    assert out2.already_existed is True
    # the returned certificate is the STORED (first) row, NOT the 999_999 recompute.
    assert out2.certificate["certificate"]["savings"]["net_saving_microusd"] == 1_000
    # the returned stored row has envelope shape (no leaked DynamoDB pk/sk).
    assert "pk" not in out2.certificate and "sk" not in out2.certificate


def test_collision_readback_none_raises(dynamodb_mock, monkeypatch):
    """If the write-once collision read-back returns None (an inconsistent store),
    issue_and_store must RAISE, never return issued=True with certificate=None
    (Fable slice-4 close: same audit-API-lie class as (e)-1)."""
    cs.store_certificate(_good_envelope())     # make the row exist -> next issue collides
    # force the read-back to miss.
    monkeypatch.setattr(cs, "get_certificate", lambda **kw: None)
    with pytest.raises(RuntimeError, match="could not be read back"):
        cs.issue_and_store(tenant_id="acme", day="20260717", generated_at_ms=9,
                           certificate_fn=_fake_certificate_fn(_cert()))


def test_store_refuses_synthetic(dynamodb_mock):
    env = _good_envelope()
    env["certificate"]["traffic"] = "synthetic"
    with pytest.raises(ValueError, match="non-real"):
        cs.store_certificate(env)


def test_store_refuses_missing_quality_caveat(dynamodb_mock):
    env = _good_envelope()
    env["certificate"]["savings"]["quality"]["measured"] = True    # claims measured
    with pytest.raises(ValueError, match="caveat"):
        cs.store_certificate(env)


def test_store_refuses_missing_potential_note(dynamodb_mock):
    env = _good_envelope()
    del env["certificate"]["savings"]["potential"]["note"]
    with pytest.raises(ValueError, match="caveat"):
        cs.store_certificate(env)


def test_get_absent_certificate_is_none(dynamodb_mock):
    assert cs.get_certificate(tenant_id="nobody", day="20260101") is None


def test_real_savings_output_satisfies_the_store_caveat_contract():
    """Round-trip contract (Fable slice-4 (b)): the ACTUAL savings.summarize_savings
    output must satisfy the store's honesty-caveat invariant. If a future savings
    schema change drops quality.measured/potential.note, this trips here — coupling
    the two modules so the change author sees the break, not a silent drift."""
    from mvp.learning.savings import summarize_savings
    savings = summarize_savings([])                     # empty is fine; shape is what matters
    # the store accepts a certificate carrying THIS real savings block.
    assert cs._has_required_caveats({"savings": savings}) is True
    # and rejects it the moment the caveat is removed (proves the check bites).
    broken = dict(savings)
    broken["quality"] = {"measured": True}
    assert cs._has_required_caveats({"savings": broken}) is False


def test_issue_and_store_persists_good_day(dynamodb_mock):
    out = cs.issue_and_store(tenant_id="acme", day="20260717", generated_at_ms=7,
                             certificate_fn=_fake_certificate_fn(_cert()))
    assert out.issued is True
    assert cs.get_certificate(tenant_id="acme", day="20260717") is not None


def test_issue_and_store_skips_empty_day_without_writing(dynamodb_mock):
    out = cs.issue_and_store(tenant_id="acme", day="20260717", generated_at_ms=7,
                             certificate_fn=_fake_certificate_fn(_cert(vsr_acted=0)))
    assert out.issued is False
    assert cs.get_certificate(tenant_id="acme", day="20260717") is None


# ---------------------------------------------------------------- batch scheduler

def test_batch_isolates_per_tenant_failure(dynamodb_mock):
    def _fn(*, tenant_id, day, traffic):
        if tenant_id == "boom":
            raise RuntimeError("reconcile blew up")
        c = dict(_cert(vsr_acted=(0 if tenant_id == "quiet" else 10)))
        c["tenant_id"], c["day"], c["traffic"] = tenant_id, day, traffic
        return c
    rep = cs.issue_for_tenants(tenant_ids=["ok", "boom", "quiet"], day="20260717",
                               generated_at_ms=1, certificate_fn=_fn)
    assert rep.issued == ["ok"]
    assert rep.skipped == [("quiet", cs.SKIP_NO_TRAFFIC)]
    assert rep.failed == [("boom", "RuntimeError")]
    # the healthy tenant was persisted despite the sibling failure.
    assert cs.get_certificate(tenant_id="ok", day="20260717") is not None


# ---------------------------------------------------------------- CLI

def test_cli_get_missing_is_error(dynamodb_mock, capsys):
    from mvp.learning import certificate_cli as cli
    assert cli.main(["get", "--tenant", "acme", "--day", "20260717"]) == 1
    assert "no certificate" in capsys.readouterr().err


def test_cli_issue_requires_generated_at(dynamodb_mock, capsys):
    from mvp.learning import certificate_cli as cli
    # issue without --generated-at-ms is refused (no implicit clock).
    assert cli.main(["issue", "--tenant", "acme", "--day", "20260717"]) == 2
    assert "generated-at-ms is required" in capsys.readouterr().err
