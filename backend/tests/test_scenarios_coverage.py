"""CI guard for the workshop scenarios (scenarios/).

Three jobs, all so the workshops stay HONEST as the code evolves:

  1. lint every coverage.yaml (valid states; not-implemented carries an issue;
     a step that claims to measure must run on shipped code).
  2. keep scenarios/COVERAGE.md in sync — regenerate and assert byte-equality, so
     a hand-edit or a stale checkout fails here (the file says DO NOT EDIT).
  3. pin the numbers that appear in each scenario's prose to what its run.py
     actually produces, so a doc can never silently drift from the code (same
     posture as the Savings demo doc-pinning test).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCEN = _REPO / "scenarios"
_COV_MOD = _SCEN / "_schema" / "coverage.py"
_SMALL_TEAM_RUN = _SCEN / "usage" / "small-team" / "run.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses with `from __future__ import annotations`
    # resolve field annotations against sys.modules[cls.__module__] at class
    # creation, which fails for a by-path import that was never registered.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cov():
    if not _COV_MOD.is_file():
        pytest.skip("coverage module not found")
    return _load(_COV_MOD, "scenarios_coverage")


@pytest.fixture(scope="module")
def small_team():
    if not _SMALL_TEAM_RUN.is_file():
        pytest.skip("small-team run.py not found")
    return _load(_SMALL_TEAM_RUN, "small_team_run")


def test_coverage_lint_clean(cov):
    scs = cov.load_scenarios()
    assert scs, "no scenarios found — expected at least usage/small-team"
    problems = cov.lint(scs)
    assert not problems, "coverage honesty-rule violations:\n" + "\n".join(problems)


def test_coverage_md_in_sync(cov):
    """scenarios/COVERAGE.md must equal the freshly rendered matrix (it is
    auto-generated; a hand-edit or stale file fails here)."""
    scs = cov.load_scenarios()
    expected = cov.render_matrix(scs) + "\n"
    out = _SCEN / "COVERAGE.md"
    assert out.is_file(), "scenarios/COVERAGE.md missing — regenerate it"
    actual = out.read_text()
    assert actual == expected, (
        "scenarios/COVERAGE.md is stale or hand-edited. Regenerate with:\n"
        "  python -c \"import sys; sys.path.insert(0,'scenarios/_schema'); "
        "import coverage as c; scs=c.load_scenarios(); "
        "open('scenarios/COVERAGE.md','w').write(c.render_matrix(scs)+'\\n')\"")


def test_every_not_implemented_gap_links_to_gaps_file(cov):
    """Every not-implemented issue link resolves to a real anchor in GAPS.md, so
    the roadmap pointers are never dangling."""
    scs = cov.load_scenarios()
    gaps_md = (_SCEN / "GAPS.md")
    text = gaps_md.read_text() if gaps_md.is_file() else ""
    for sc in scs:
        for st in sc.steps:
            if st.state == cov.NOT_IMPLEMENTED and st.issue:
                if st.issue.startswith("scenarios/GAPS.md#"):
                    anchor = st.issue.split("#", 1)[1]
                    # GitHub slugifies headings to lowercase-with-dashes; our
                    # anchors are already in that form and appear as "## <anchor>".
                    assert f"## {anchor}" in text, (
                        f"{sc.path}/{st.id}: GAPS.md has no section '## {anchor}'")


def test_small_team_cost_axis_runs_today(small_team):
    """The cost axis must actually produce a certificate (the 'covered' claim)."""
    rep = small_team.build_report()
    c = rep["cost"]
    assert c["priced_request_count"] >= 1
    # escalation loss is non-zero (honest: net is dragged down, not cherry-picked)
    assert c["decomposition"]["negative_deltas_microusd"] > 0
    assert c["quality_measured"] is False
    # potential (shadow) is separate, never the headline
    assert c["potential_net_microusd"] != c["net_saving_microusd"]


def test_small_team_perf_axis_is_an_honest_gap(small_team):
    """The perf axis must NOT invent a TTFT/TPOT number — it is a declared gap."""
    p = small_team.build_report()["perf"]
    assert p["ttft_ms"] is None and p["tpot_ms"] is None
    assert p["gap"] == "not-implemented"
    assert p["issue"].startswith("scenarios/GAPS.md#")


def test_small_team_quality_scorer_is_conservative(small_team):
    """Exact-match, conservative: the comma-formatted '1,024' and the blank answer
    both count as NOT correct -> 8/10, not 10/10."""
    q = small_team.build_report()["quality"]
    assert q["graded"] == 10
    assert q["correct"] == 8
    assert abs(q["accuracy"] - 0.8) < 1e-9
    assert "exact-match" in q["method"]
    # the eval tap that would feed real traffic is a declared gap
    assert q["tap_gap"]["gap"] == "not-implemented"


def _usd(micro: int) -> str:
    n = abs(int(micro))
    return f"{'-' if micro < 0 else ''}${n // 1_000_000}.{n % 1_000_000:06d}"


@pytest.mark.parametrize("doc", ["scenario.md", "scenario_ja.md"])
def test_small_team_doc_numbers_pinned(small_team, doc):
    """Both the English and Japanese scenario docs quote figures that MUST equal
    run.py's current output — the docs cannot silently drift."""
    path = _SMALL_TEAM_RUN.parent / doc
    if not path.is_file():
        pytest.skip(f"{doc} not found")
    text = path.read_text()
    c = small_team.build_report()["cost"]
    for micro in (c["net_saving_microusd"],
                  c["decomposition"]["positive_deltas_microusd"],
                  c["decomposition"]["negative_deltas_microusd"],
                  c["potential_net_microusd"]):
        assert _usd(micro) in text, f"{doc}: figure {_usd(micro)} not found (drifted?)"
    q = small_team.build_report()["quality"]
    assert f"{q['correct']}/{q['graded']}" in text


def test_live_evidence_has_committed_results(cov):
    """Any step marked with live `evidence` must be backed by a committed results
    file (results/live-<run_id>.json) — a live claim cannot be unbacked."""
    scs = cov.load_scenarios()
    checked = 0
    for sc in scs:
        for st in sc.steps:
            if st.evidence and st.evidence.get("mode") == "live":
                run_id = st.evidence.get("run_id")
                assert run_id, f"{sc.path}/{st.id}: live evidence missing run_id"
                results = (_SCEN / sc.path / "results" / f"live-{run_id}.json")
                assert results.is_file(), (
                    f"{sc.path}/{st.id}: live evidence run_id={run_id} has no "
                    f"committed {results.relative_to(_SCEN)}")
                data = json.loads(results.read_text())
                assert data.get("provenance", {}).get("source") == "real"
                checked += 1
    assert checked >= 1, "expected at least one live-evidenced step (small-team)"


def test_live_results_are_honestly_stamped(cov):
    """The committed live results carry full provenance and declare themselves a
    gateway-NOT-in-path baseline (never a 'gateway verified' claim)."""
    results = _SCEN / "usage" / "small-team" / "results" / "live-demo1.json"
    if not results.is_file():
        pytest.skip("live-demo1.json not committed")
    data = json.loads(results.read_text())
    prov = data["provenance"]
    assert prov["source"] == "real"
    assert prov["gateway_in_path"] is False           # honest: baseline, not gateway
    for key in ("model_id", "region", "timestamp", "run_id", "reps_per_task"):
        assert prov.get(key), f"provenance missing {key}"
    # raw runs are kept (not averaged away); N matches
    assert data["perf"]["ttft_ms_raw"], "raw TTFT runs must be kept"
    assert len(data["perf"]["ttft_ms_raw"]) == data["perf"]["n_calls"]


@pytest.mark.parametrize("doc", ["scenario.md", "scenario_ja.md"])
def test_small_team_doc_live_quotes_match_committed_results(doc):
    """The live figures quoted in the docs must match the COMMITTED results file
    (results/live-demo1.json), NOT a fresh run — live numbers are non-deterministic
    and CI never re-runs them, but the doc must not drift from its evidence."""
    base = _SCEN / "usage" / "small-team"
    results = base / "results" / "live-demo1.json"
    doc_path = base / doc
    if not (results.is_file() and doc_path.is_file()):
        pytest.skip("live results or doc missing")
    data = json.loads(results.read_text())
    text = doc_path.read_text()
    cost = data["cost"]["total_billed_microusd"]
    assert _usd(cost) in text, f"{doc}: live cost {_usd(cost)} not quoted"
    p = data["perf"]
    for v in (p["ttft_ms_p50"], p["ttft_ms_min"], p["ttft_ms_max"]):
        assert str(v) in text, f"{doc}: live TTFT {v} not quoted"
    assert f"N={p['n_calls']}" in text
    q = data["quality"]
    assert f"{q['correct']}/{q['graded']}" in text


def test_offline_and_live_share_one_scorer(small_team):
    """The live harness MUST reuse run.py's scorer/normalizer so the grade cannot
    drift between offline and live modes."""
    live_path = _SMALL_TEAM_RUN.parent / "live.py"
    if not live_path.is_file():
        pytest.skip("live.py not present")
    live = _load(live_path, "small_team_live")
    # live imports run.py as `offline`; assert both resolve to the SAME source file
    # (by-path test imports create distinct module objects, so compare by origin).
    assert live.offline.__file__ == small_team.__file__ == str(_SMALL_TEAM_RUN)
    # and that live actually calls the shared scorer/normalizer (not a copy).
    assert live.offline.score_exact_match.__module__ == live.offline.normalize_answer.__module__
    assert "score_exact_match" in dir(live.offline)
    assert "normalize_answer" in dir(live.offline)
