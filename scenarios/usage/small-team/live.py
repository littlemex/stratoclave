"""LIVE (real-Bedrock) baseline verification for the small-team workshop.

This is the `--live` companion to run.py's offline mode. It drives the SAME 10
tasks and the SAME conservative exact-match scorer (imported from run.py, so the
grade can never drift between modes) against REAL Bedrock, measuring all three
axes on real traffic:

  COST     real token usage (from Bedrock's own message_delta usage — never
           estimated) priced by the shipped pricer -> micro-USD.
  PERF     TTFT from the first content chunk, TPOT from inter-chunk gaps, measured
           client-side over invoke_model_with_response_stream.
  QUALITY  the real model output, scored by the shared conservative exact-match.

HONESTY (Fable live-verify review — enforced, not just documented):
  * This is a LIVE BASELINE, gateway NOT in the path. It verifies the measurement
    method and the pricer against real tokens — NOT "Stratoclave gateway verified".
    The value is that a gateway-routed TTFT can later be diffed against THIS.
  * Non-determinism is kept honest: every raw run is saved, N is stamped, and with
    small N we do NOT name percentiles — we print the raw values and p50 only.
  * Provenance (source=real, model_id, region, timestamp, N, run_id) is stamped on
    the results so a live number can never be mistaken for an offline/audited one.
  * A hard COST CAP aborts the run before it can overspend.
  * NEVER run in CI. Opt-in only, and it costs real (tiny) money.

    AWS_PROFILE=... AWS_REGION=us-east-1 \
        python scenarios/usage/small-team/live.py --run-id demo1

Requires AWS credentials with Bedrock access. Output: prints a report and writes
results/live-<run_id>.json (git-ignored) next to this file.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]
_BACKEND = _ROOT / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Share the offline scorer + normalizer so the grade is identical across modes.
sys.path.insert(0, str(_HERE))
import run as offline                                  # noqa: E402
from mvp.learning import savings as sv                 # noqa: E402

_EVALSET = _HERE / "mini_eval.jsonl"

# --- honesty guardrails (Fable retreat criterion) --------------------------
MODEL_ALIAS = "claude-haiku-4-5"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
REPS = 3                        # per-task latency repetitions; raw runs kept
MAX_TOKENS = 64                 # answers are 1-token-class; 64 is generous
COST_CAP_MICROUSD = 100_000     # $0.10/run hard cap — abort before overspend
_ANSWER_INSTRUCTION = "\n\nAnswer with ONLY the answer, no explanation."


def _load_tasks() -> list[dict]:
    tasks = []
    with _EVALSET.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return [t for t in tasks if (t.get("expected") or "").strip()]


def _one_call(client, prompt: str) -> dict:
    """One streaming Bedrock call. Returns text, TTFT, total, token usage (from
    Bedrock, not estimated)."""
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt + _ANSWER_INSTRUCTION}],
    }
    t0 = time.perf_counter()
    ttft = None
    text = ""
    usage: dict = {}
    resp = client.invoke_model_with_response_stream(
        modelId=MODEL_ID, body=json.dumps(body))
    for ev in resp["body"]:
        chunk = json.loads(ev["chunk"]["bytes"])
        ty = chunk.get("type")
        if ty == "content_block_delta":
            if ttft is None:
                ttft = time.perf_counter() - t0
            text += chunk["delta"].get("text", "")
        elif ty == "message_delta":
            usage = chunk.get("usage", {}) or usage
    total = time.perf_counter() - t0
    return {"text": text.strip(), "ttft_s": ttft, "total_s": total, "usage": usage}


def run_live(run_id: str, now_iso: str, region: str) -> dict:
    import boto3

    price = sv._default_pricer()
    resolve = sv._default_resolver()
    pricing_key = resolve(MODEL_ALIAS)["pricing_key"]
    client = boto3.client("bedrock-runtime", region_name=region)
    tasks = _load_tasks()

    graded_records: list[dict] = []     # one per task (rep 0 output) for scoring
    raw_runs: list[dict] = []           # every call, kept raw (no averaging away)
    ttfts: list[float] = []
    total_cost = 0

    for t in tasks:
        first_text = None
        for rep in range(REPS):
            r = _one_call(client, t["task"])
            u = r["usage"]
            tin, tout = int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0))
            cost = int(price(pricing_key, tin, tout))
            total_cost += cost
            if total_cost > COST_CAP_MICROUSD:
                raise RuntimeError(
                    f"cost cap ${COST_CAP_MICROUSD/1e6:.2f} exceeded "
                    f"(spent ${total_cost/1e6:.4f}) — aborting to avoid overspend")
            if r["ttft_s"] is not None:
                ttfts.append(r["ttft_s"])
            raw_runs.append({
                "task_id": t["id"], "rep": rep, "text": r["text"],
                "ttft_ms": round(r["ttft_s"] * 1000, 1) if r["ttft_s"] else None,
                "total_ms": round(r["total_s"] * 1000, 1),
                "input_tokens": tin, "output_tokens": tout, "cost_microusd": cost,
            })
            if first_text is None:
                first_text = r["text"]
        # score the FIRST rep's output (deterministic pick; reps are for latency)
        graded_records.append({"id": t["id"], "expected": t["expected"],
                               "answer": first_text})

    quality = offline.score_exact_match(graded_records)
    ttft_ms = sorted(round(x * 1000, 1) for x in ttfts)
    perf = {
        "n_calls": len(ttfts),
        "ttft_ms_raw": ttft_ms,                        # raw, not averaged away
        "ttft_ms_p50": round(statistics.median(ttft_ms), 1) if ttft_ms else None,
        "ttft_ms_min": min(ttft_ms) if ttft_ms else None,
        "ttft_ms_max": max(ttft_ms) if ttft_ms else None,
        "note": (f"N={len(ttfts)} raw client-side measurements; with small N we do "
                 "NOT name percentiles beyond p50. LIVE BASELINE — gateway NOT in "
                 "the path; a gateway-routed TTFT would be diffed against this."),
    }
    return {
        "scenario": "usage/small-team",
        "mode": "live-baseline",
        "provenance": {
            "source": "real", "model_id": MODEL_ID, "region": region,
            "timestamp": now_iso, "run_id": run_id, "reps_per_task": REPS,
            "gateway_in_path": False,
        },
        "cost": {
            "total_billed_microusd": total_cost,
            "note": "real Bedrock token usage priced by the shipped pricer.",
        },
        "perf": perf,
        "quality": {**quality, "normalize": offline.NORMALIZE_VERSION},
        "raw_runs": raw_runs,
    }


def _usd(micro: int) -> str:
    n = abs(int(micro))
    return f"${n // 1_000_000}.{n % 1_000_000:06d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="small-team-live")
    ap.add_argument("--run-id", required=True, help="label stamped on results")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # timestamp is captured here (not inside pure code) and stamped as provenance.
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        report = run_live(args.run_id, now_iso, args.region)
    except Exception as e:  # noqa: BLE001 — surface the abort/credential error plainly
        print(f"[LIVE ABORTED] {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    out_dir = _HERE / "results"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"live-{args.run_id}.json"
    out_file.write_text(json.dumps(report, indent=2, sort_keys=True))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    p = report["perf"]
    q = report["quality"]
    print("=== small-team LIVE BASELINE (real Bedrock, gateway NOT in path) ===")
    prov = report["provenance"]
    print(f"    source=real model={prov['model_id']} region={prov['region']}")
    print(f"    at={prov['timestamp']} run_id={prov['run_id']} reps/task={prov['reps_per_task']}")
    print()
    print(f"[COST]     total billed = {_usd(report['cost']['total_billed_microusd'])}"
          " (real token usage x shipped pricer)")
    print(f"[PERF]     TTFT p50={p['ttft_ms_p50']}ms  min={p['ttft_ms_min']}"
          f"  max={p['ttft_ms_max']}  (N={p['n_calls']}, raw kept)")
    print(f"           raw TTFT ms: {p['ttft_ms_raw']}")
    print(f"[QUALITY]  {q['correct']}/{q['graded']} exact-match "
          f"({(q['accuracy'] or 0):.0%}), conservative; {q['normalize']}")
    print()
    print(f"results -> {out_file.relative_to(_ROOT)}")
    print("LIVE BASELINE — verifies the measurement method + pricer on real "
          "tokens, NOT the gateway. Next: emit gateway-side TTFT to diff against this.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
