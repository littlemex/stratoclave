"""Unit tests for the Savings Certificate CLI (mvp.learning.savings_cli).

Verifies the human-readable + JSON rendering, the honest-denominator output, and
that a NET LOSS renders with a leading '-' (never hidden). The certificate data
is stubbed (monkeypatched) so no DynamoDB is touched — this is a pure rendering
test; the numbers themselves are proven in test_savings* .
"""
from __future__ import annotations

import json

import pytest

from mvp.learning import savings_cli
from mvp.learning import savings as sv


def _fake_cert(net, positive, negative, *, classes=None, rate="v-9", traffic="real"):
    return {
        "tenant_id": "acme", "day": "20260720", "rate_version": rate,
        "traffic": traffic,
        "savings": {
            "net_saving_microusd": net,
            "priced_request_count": 3,
            "billed_microusd_over_priced_base": 100_000,
            "total_billed_microusd_all_classes": 500_000,
            "decomposition": {"positive_deltas_microusd": positive,
                              "negative_deltas_microusd": negative},
            "class_counts": classes or {"counterfactual": 3, "followed": 2},
            "class_billed_microusd": {"counterfactual": 100_000, "followed": 400_000},
            "quality": {"measured": False, "note": "fill from tenant eval"},
            "detail": [],
        },
        "reconcile": {},
    }


def test_fmt_usd_negative_has_leading_minus():
    assert savings_cli._fmt_usd(-9_800) == "-$0.009800"
    assert savings_cli._fmt_usd(2_500_000) == "$2.500000"


def test_json_mode_emits_full_certificate(monkeypatch, capsys):
    cert = _fake_cert(net=80_000, positive=80_000, negative=0)
    monkeypatch.setattr(sv, "savings_certificate", lambda **kw: cert)
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["savings"]["net_saving_microusd"] == 80_000
    assert parsed["rate_version"] == "v-9"


def test_human_mode_shows_net_and_loss(monkeypatch, capsys):
    # a NET LOSS must render with a minus sign — the honest sign is visible.
    cert = _fake_cert(net=-5_000, positive=1_000, negative=6_000)
    monkeypatch.setattr(sv, "savings_certificate", lambda **kw: cert)
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NET saving:" in out
    assert "-$0.005000" in out                 # the net loss, shown not hidden
    assert "dearer-if-followed:  $0.006000" in out
    # honesty: the quality-not-measured caveat is always printed.
    assert "quality measured:         False" in out
    assert "QUALITY IS NOT MEASURED" in out    # NOTICE block
    # scope declaration: the reader never mistakes net% for a whole-traffic figure.
    assert "VSR-acted requests only" in out


def test_human_mode_reports_class_counts(monkeypatch, capsys):
    cert = _fake_cert(net=80_000, positive=80_000, negative=0,
                      classes={"counterfactual": 3, "followed": 2, "unpriceable": 1})
    monkeypatch.setattr(sv, "savings_certificate", lambda **kw: cert)
    savings_cli.main(["--tenant", "acme", "--day", "20260720"])
    out = capsys.readouterr().out
    assert "unpriceable=1" in out and "counterfactual=3" in out


def test_traffic_flag_reaches_engine_and_defaults_real(monkeypatch, capsys):
    seen = {}

    def _spy(**kw):
        seen.update(kw)
        return _fake_cert(net=1, positive=1, negative=0, traffic=kw.get("traffic", "real"))

    monkeypatch.setattr(sv, "savings_certificate", _spy)
    # default is real, and a real certificate shows NO synthetic banner.
    savings_cli.main(["--tenant", "acme", "--day", "20260720"])
    assert seen["traffic"] == "real"
    assert "SEEDED SAMPLE" not in capsys.readouterr().out


def test_synthetic_traffic_loudly_banners(monkeypatch, capsys):
    monkeypatch.setattr(sv, "savings_certificate",
                        lambda **kw: _fake_cert(net=1, positive=1, negative=0,
                                                traffic=kw.get("traffic", "real")))
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720", "--traffic", "synthetic"])
    out = capsys.readouterr().out
    assert rc == 0
    # a synthetic sample can NEVER be mistaken for a real audited number.
    assert "TRAFFIC: SYNTHETIC" in out and "NOT A REAL" in out


# ------------------------------------------------------------------ C2.3 replay


def test_replay_passes_the_prior_basis_and_reports_agreement(monkeypatch, capsys, tmp_path):
    """`--replay` is the operator-visible half of C2.3: it hands a previous report's
    own rate and model tables back to the computation, so the figure is reproduced
    rather than recomputed at today's prices."""
    prior = _fake_cert(net=80_000, positive=80_000, negative=0)
    prior["rate_table"] = {"claude-opus-4-7": {"input_per_mtok_microusd": 1}}
    prior["model_table"] = {"claude-opus-4-7": {"pricing_key": "opus",
                                               "bedrock_model_id": "us.x"}}
    path = tmp_path / "cert.json"
    path.write_text(json.dumps(prior))

    seen: dict = {}

    def _fake(**kw):
        seen.update(kw)
        return prior

    monkeypatch.setattr(sv, "savings_certificate", _fake)
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720",
                          "--replay", str(path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["rate_table"] == prior["rate_table"]
    assert seen["model_table"] == prior["model_table"]
    assert "REPLAY OK" in out


def test_replay_reports_a_mismatch_as_a_failure(monkeypatch, capsys, tmp_path):
    """A replay that does not reproduce the figure exits non-zero. The facts changed
    (late-settled usage, a re-reconciled day) — the rates did not, which is exactly
    what makes the disagreement worth surfacing rather than printing quietly."""
    prior = _fake_cert(net=80_000, positive=80_000, negative=0)
    prior["rate_table"] = {}
    prior["model_table"] = {}
    path = tmp_path / "cert.json"
    path.write_text(json.dumps(prior))

    monkeypatch.setattr(sv, "savings_certificate",
                        lambda **kw: _fake_cert(net=70_000, positive=70_000, negative=0))
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720",
                          "--replay", str(path)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "REPLAY MISMATCH" in out


def test_replay_accepts_a_stored_envelope(monkeypatch, capsys, tmp_path):
    """A persisted certificate is wrapped in an envelope (`certificate`, plus the
    revision/supersedes chain), and that is the file an operator actually has. Both
    shapes are accepted so reproducing a stored figure does not require unwrapping
    it by hand first."""
    prior = _fake_cert(net=80_000, positive=80_000, negative=0)
    prior["rate_table"] = {"k": {"input_per_mtok_microusd": 1}}
    prior["model_table"] = {"m": None}
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps({"record_type": "savings_certificate", "revision": 0,
                                "certificate": prior}))

    seen: dict = {}
    monkeypatch.setattr(sv, "savings_certificate",
                        lambda **kw: (seen.update(kw), prior)[1])
    rc = savings_cli.main(["--tenant", "acme", "--day", "20260720",
                          "--replay", str(path)])
    assert rc == 0
    assert seen["rate_table"] == prior["rate_table"]
    assert "REPLAY OK" in capsys.readouterr().out
