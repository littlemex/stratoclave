"""Guards the offline Savings Certificate demo (bench/savings/demo_offline.py) so
the checked-in docs (docs/demo/savings-vs-litellm.md) can never silently drift
from what the real engine actually produces.

The demo is a COURTESY sample, but a sample whose whole point is honesty must stay
truthful in CI: if a rate table or model registry changes, the numbers quoted in
the doc must fail here rather than quietly become a lie.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BENCH = Path(__file__).resolve().parents[2] / "bench" / "savings" / "demo_offline.py"


@pytest.fixture(scope="module")
def demo():
    """Import the bench script by path (it is outside the backend package)."""
    if not _BENCH.is_file():
        pytest.skip(f"demo script not found at {_BENCH}")
    spec = importlib.util.spec_from_file_location("demo_offline", _BENCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_workload_loads_and_prices(demo):
    """Every row prices with the REAL pricer; no hand-authored cost sneaks in."""
    price = demo.sv._default_pricer()
    resolve = demo.sv._default_resolver()
    rows = demo.price_rows(demo.load_rows(), price, resolve)
    assert len(rows) == 9
    # every steered/shadow row with a known billed model got a recomputed cost
    for r in rows:
        if resolve(str(r.get("billed_model_id") or "")):
            assert r["cost_microusd"] > 0


def test_certificate_shape_and_honest_classes(demo):
    cert = demo.build_certificate()
    s = cert["savings"]
    # provenance + refusal are present and honest
    assert cert["traffic"] == "synthetic"
    assert cert["rate_version"] == demo.BUILTIN_VERSION
    assert s["quality"]["measured"] is False
    # class shape: 6 counterfactual (3 saving + 1 loss + 2 shadow), 2 followed,
    # 1 no_suggestion. NO unpriceable / no_cost (all models resolve).
    cc = s["class_counts"]
    assert cc == {"counterfactual": 6, "followed": 2, "no_suggestion": 1}


def test_headline_excludes_potential_and_subtracts_escalation(demo):
    cert = demo.build_certificate()
    s = cert["savings"]
    # realized headline = 4 priced (shadow's 2 live in `potential`, never summed in)
    assert s["priced_request_count"] == 4
    # net = positive deltas - escalation loss, and the loss is non-zero (honest)
    decomp = s["decomposition"]
    assert decomp["negative_deltas_microusd"] > 0, "escalation loss must be non-zero"
    assert (s["net_saving_microusd"]
            == decomp["positive_deltas_microusd"] - decomp["negative_deltas_microusd"])
    # potential is a SEPARATE section, not folded into the headline
    assert s["potential"]["priced_request_count"] == 2
    assert s["potential"]["net_saving_microusd"] != s["net_saving_microusd"]


def test_reproducible_identical_net(demo):
    a = demo.build_certificate()["savings"]["net_saving_microusd"]
    b = demo.build_certificate()["savings"]["net_saving_microusd"]
    assert a == b


def test_doc_numbers_match_engine_output(demo):
    """The figures quoted in docs/demo/savings-vs-litellm.md MUST equal what the
    engine produces now — the doc's honesty guarantee, enforced."""
    doc = (Path(__file__).resolve().parents[2]
           / "docs" / "demo" / "savings-vs-litellm.md")
    if not doc.is_file():
        pytest.skip("companion doc not found")
    text = doc.read_text()

    cert = demo.build_certificate()
    s = cert["savings"]

    def usd(micro: int) -> str:
        n = abs(int(micro))
        return f"${n // 1_000_000}.{n % 1_000_000:06d}"

    # headline figures the doc prints in the certificate block + comparison table
    assert usd(s["net_saving_microusd"]) in text                    # NET saving
    assert usd(s["decomposition"]["positive_deltas_microusd"]) in text
    assert usd(s["decomposition"]["negative_deltas_microusd"]) in text
    assert usd(s["total_billed_microusd_all_classes"]) in text
    assert usd(s["billed_microusd_over_priced_base"]) in text

    # spend-log table totals (per billed model) recomputed here to match the doc
    price = demo.sv._default_pricer()
    resolve = demo.sv._default_resolver()
    spend: dict[str, int] = {}
    for r in demo.load_rows():
        b = resolve(str(r.get("billed_model_id") or ""))
        if b:
            spend[r["billed_model_id"]] = spend.get(r["billed_model_id"], 0) + int(
                price(b["pricing_key"], int(r["input_tokens"]), int(r["output_tokens"])))
    for model, total in spend.items():
        assert usd(total) in text, f"spend-log total for {model} ({usd(total)}) not in doc"
