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
