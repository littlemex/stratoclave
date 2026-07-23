"""GATEWAY-PATH live verification for the small-team workshop — the real thing.

This drives the workshop's 10 tasks THROUGH the Stratoclave gateway's own request
path (`POST /v1/messages`: auth -> reserve -> real Bedrock converse -> settle ->
ledger) to REAL Bedrock, and measures all three axes on data that actually crossed
the gateway:

  COST     the CHARGE-OF-RECORD the gateway settled into the ledger (the pool's
           settled delta per request), NOT a client-side estimate. This is the
           number only a gateway with a real ledger can produce.
  PERF     gateway-path TTFT/TPOT (first SSE content delta), measured PAIRED against
           the direct-Bedrock baseline in the SAME run so their difference is the
           gateway overhead (auth + reserve + ASGI dispatch), not cross-run noise.
  QUALITY  the gateway's response text, scored by the SAME shared exact-match
           scorer as offline/baseline.

HONEST LABELS (Fable gateway-live review — stamped in the results, never hidden):
  * transport = "in-process-asgi": the gateway's CODE PATH is exercised end to end,
    but there is NO network / ALB / TLS / process boundary. This verifies gateway
    logic + billing accuracy, NOT a deployed environment's latency. `excluded` lists
    what is not in the path.
  * ledger = "moto (not real DynamoDB)": the settle value is the charge-of-record
    the gateway CODE wrote; real DynamoDB's behaviour is not exercised here.
  * paired overhead: N is small -> a POINT ESTIMATE only (median/min/max of the
    per-task paired difference). No confidence intervals, no significance claims.
  * a hard COST CAP aborts before overspend; NEVER run in CI.

This module holds the measurement logic; `backend/tests/test_live_gateway.py`
(opt-in, SC_GW_LIVE=1) drives it with the conftest moto fixtures + a real Bedrock
client built before moto starts. Results are written to results/live-gateway-*.json.
"""
from __future__ import annotations

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

sys.path.insert(0, str(_HERE))
import run as offline                                 # noqa: E402  shared scorer
from mvp.learning import savings as sv                # noqa: E402

_EVALSET = _HERE / "mini_eval.jsonl"

MODEL_ALIAS = "claude-haiku-4-5"
MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
MAX_TOKENS = 64
COST_CAP_MICROUSD = 1_000_000       # $1.00 hard cap across the whole run
_ANSWER_INSTRUCTION = "\n\nAnswer with ONLY the answer, no explanation."
_EXCLUDED = ["network", "ALB", "TLS", "process-boundary", "CloudFront/WAN"]


def _load_tasks() -> list[dict]:
    tasks = []
    with _EVALSET.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return [t for t in tasks if (t.get("expected") or "").strip()]


def _direct_call(bedrock, prompt: str) -> dict:
    """One DIRECT streaming call (gateway NOT in path) — the baseline leg."""
    body = {"anthropic_version": "bedrock-2023-05-31", "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt + _ANSWER_INSTRUCTION}]}
    t0 = time.perf_counter()
    ttft = None
    text = ""
    usage: dict = {}
    resp = bedrock.invoke_model_with_response_stream(
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
    return {"text": text.strip(), "ttft_s": ttft,
            "input_tokens": int(usage.get("input_tokens", 0)),
            "output_tokens": int(usage.get("output_tokens", 0))}


def _gateway_call(client, prompt: str) -> dict:
    """One GATEWAY-PATH streaming call through POST /v1/messages. TTFT is the first
    content_block_delta SSE frame."""
    t0 = time.perf_counter()
    ttft = None
    text = ""
    with client.stream("POST", "/v1/messages", json={
        "model": MODEL_ALIAS,
        "messages": [{"role": "user", "content": prompt + _ANSWER_INSTRUCTION}],
        "max_tokens": MAX_TOKENS, "stream": True,
    }) as s:
        if s.status_code != 200:
            raise RuntimeError(f"gateway {s.status_code}: {s.read()[:200]!r}")
        for line in s.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            if ev.get("type") == "content_block_delta":
                if ttft is None:
                    ttft = time.perf_counter() - t0
                text += ev.get("delta", {}).get("text", "")
    return {"text": text.strip(), "ttft_s": ttft}


def run_gateway_verification(*, client, bedrock, pool_summary, run_id: str,
                             now_iso: str, region: str) -> dict:
    """Drive the tasks PAIRED (direct then gateway, order alternated per task to
    cancel warm-up bias). Reads the ledger charge-of-record via the pool_summary
    callback (settled delta per gateway call). Returns the honest report dict.

    `pool_summary()` -> dict with pool_settled_microusd (the ledger read).
    `bedrock` is a real client; `client` is the in-process TestClient.
    """
    price = sv._default_pricer()
    resolve = sv._default_resolver()
    pricing_key = resolve(MODEL_ALIAS)["pricing_key"]
    tasks = _load_tasks()

    pairs: list[dict] = []
    graded_gateway: list[dict] = []
    gateway_ttfts: list[float] = []
    direct_ttfts: list[float] = []
    client_priced_total = 0
    ledger_settled_total_before = int(pool_summary().get("pool_settled_microusd", 0))

    for i, t in enumerate(tasks):
        # alternate order to cancel warm-up bias in the paired difference
        direct_first = (i % 2 == 0)
        settled_before = int(pool_summary().get("pool_settled_microusd", 0))

        if direct_first:
            d = _direct_call(bedrock, t["task"])
            g = _gateway_call(client, t["task"])
        else:
            g = _gateway_call(client, t["task"])
            d = _direct_call(bedrock, t["task"])

        settled_after = int(pool_summary().get("pool_settled_microusd", 0))
        ledger_charge = settled_after - settled_before   # gateway's charge-of-record
        client_est = int(price(pricing_key, d["input_tokens"], d["output_tokens"]))
        client_priced_total += client_est

        if d["ttft_s"] is not None:
            direct_ttfts.append(round(d["ttft_s"] * 1000, 1))
        if g["ttft_s"] is not None:
            gateway_ttfts.append(round(g["ttft_s"] * 1000, 1))
        pairs.append({
            "task_id": t["id"], "order": "direct-first" if direct_first else "gateway-first",
            "direct_ttft_ms": round(d["ttft_s"] * 1000, 1) if d["ttft_s"] else None,
            "gateway_ttft_ms": round(g["ttft_s"] * 1000, 1) if g["ttft_s"] else None,
            "overhead_ms": (round((g["ttft_s"] - d["ttft_s"]) * 1000, 1)
                            if (g["ttft_s"] and d["ttft_s"]) else None),
            "ledger_charge_microusd": ledger_charge,
            "client_estimate_microusd": client_est,
            "gateway_text": g["text"], "direct_text": d["text"],
        })
        # score the GATEWAY response (that is the path under test)
        graded_gateway.append({"id": t["id"], "expected": t["expected"],
                               "answer": g["text"]})

        if client_priced_total > COST_CAP_MICROUSD:
            raise RuntimeError(
                f"cost cap ${COST_CAP_MICROUSD/1e6:.2f} exceeded — aborting")

    quality = offline.score_exact_match(graded_gateway)
    overheads = [p["overhead_ms"] for p in pairs if p["overhead_ms"] is not None]
    ledger_settled_total_after = int(pool_summary().get("pool_settled_microusd", 0))

    return {
        "scenario": "usage/small-team",
        "mode": "live-gateway",
        "path": "gateway (/v1/messages: auth -> reserve -> Bedrock -> settle -> ledger)",
        "transport": "in-process-asgi",
        "excluded": _EXCLUDED,
        "ledger": "moto (not real DynamoDB)",
        "provenance": {"source": "real", "model_id": MODEL_ID, "region": region,
                       "timestamp": now_iso, "run_id": run_id,
                       "gateway_in_path": True},
        "cost": {
            "charge_of_record_microusd": ledger_settled_total_after - ledger_settled_total_before,
            "client_side_estimate_microusd": client_priced_total,
            "note": ("charge_of_record is the ledger's settled delta the GATEWAY "
                     "wrote (moto ledger, real Bedrock usage); the client estimate "
                     "is the direct-path pricing for comparison."),
        },
        "perf": {
            "n_pairs": len(pairs),
            "gateway_ttft_ms_p50": round(statistics.median(gateway_ttfts), 1) if gateway_ttfts else None,
            "direct_ttft_ms_p50": round(statistics.median(direct_ttfts), 1) if direct_ttfts else None,
            "overhead_ms_paired_median": round(statistics.median(overheads), 1) if overheads else None,
            "overhead_ms_min": min(overheads) if overheads else None,
            "overhead_ms_max": max(overheads) if overheads else None,
            "note": (f"paired direct-vs-gateway, N={len(overheads)} — POINT ESTIMATE "
                     "only, no distribution/significance claim. overhead = gateway "
                     "TTFT - direct TTFT in the same run (auth+reserve+ASGI dispatch; "
                     "no network/ALB — see transport/excluded)."),
        },
        "quality": {**quality, "normalize": offline.NORMALIZE_VERSION,
                    "scored": "gateway-path response"},
        "pairs": pairs,
    }


def _usd(micro: int) -> str:
    n = abs(int(micro))
    return f"{'-' if micro < 0 else ''}${n // 1_000_000}.{n % 1_000_000:06d}"


def format_report(report: dict) -> str:
    c, p, q = report["cost"], report["perf"], report["quality"]
    prov = report["provenance"]
    lines = [
        "=== small-team GATEWAY-PATH live (real Bedrock, gateway IN path) ===",
        f"    transport={report['transport']}  ledger={report['ledger']}",
        f"    excluded={report['excluded']}",
        f"    source=real model={prov['model_id']} region={prov['region']} "
        f"at={prov['timestamp']} run_id={prov['run_id']}",
        "",
        f"[COST]     charge-of-record (ledger settle) = "
        f"{_usd(c['charge_of_record_microusd'])}  "
        f"(client-side estimate {_usd(c['client_side_estimate_microusd'])})",
        f"[PERF]     gateway TTFT p50={p['gateway_ttft_ms_p50']}ms  "
        f"direct TTFT p50={p['direct_ttft_ms_p50']}ms",
        f"           paired overhead median={p['overhead_ms_paired_median']}ms "
        f"(min={p['overhead_ms_min']} max={p['overhead_ms_max']}, N={p['n_pairs']}, point est.)",
        f"[QUALITY]  {q['correct']}/{q['graded']} exact-match "
        f"({(q['accuracy'] or 0):.0%}), conservative, on the gateway response",
        "",
        "GATEWAY VERIFIED: auth->reserve->real Bedrock->settle->ledger ran on real "
        "data. Charge-of-record is the ledger value the gateway wrote. TTFT excludes "
        "network (in-process ASGI) — a logic/billing check, not a deployment SLO.",
    ]
    return "\n".join(lines)
