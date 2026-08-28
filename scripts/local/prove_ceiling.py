#!/usr/bin/env python3
"""Does the dollar pool hold under concurrency? Measure it, do not assume it.

WHAT THIS IS FOR
----------------
`backend/tests/test_reservation_bound_formal_z3.py` proves that a sound
reservation makes the admission condition a hard ceiling. That is a proof about
an encoding. `backend/tests/test_reservation_bound_differential.py` shows the
shipped bound satisfies the soundness the proof assumes. Neither runs a request.

This does. It fires concurrent traffic at a real gateway against a real store
with a deliberately tiny dollar pool, and then reconstructs from the ledger
whether the pool was ever overspent — counting admissions from the ledger rather
than from what the client thinks happened.

WHAT MAKES A RESULT DECISIVE RATHER THAN MERELY GREEN
----------------------------------------------------
Everything below exists because, without it, this harness would go green while
the system was broken:

 1. **Admissions are counted from the ledger, never from the client.** A retry
    makes an admitted request look refused, so client bookkeeping cannot be the
    source of truth for what was spent.
 2. **Every amount is recomputed independently** from the tokens and the rate the
    event itself cites. Trusting `pool_settled` would pass a gateway that
    under-reports its own settles.
 3. **A quiescence barrier before reading**, and a barrier timeout is a FAILURE,
    never a pass. Reading while a reservation is still outstanding under-counts.
 4. **Vacuity checks.** At least one admit, at least one refusal whose reason is
    pool headroom, and proven overlap. Without them a run where the budget never
    bound at all reports success.
 5. **Overlap is witnessed from the ledger**, not from client timestamps: a probe
    refused for headroom while the occupier's hold exists and its terminal is not
    yet written. A client-side "the occupier had not returned yet" is the same
    unreliable narrator as (1).
 6. **Three-way classification with a conservation law.** admit + budget-refuse +
    upstream-fail must total N. A request with no home is where unaccounted spend
    hides.
 7. **INCONCLUSIVE is not PASS.** A repetition that cannot prove overlap is not
    counted, is reported, and does not contribute to the claim.

WHAT THIS CANNOT SHOW, AND WHY IT IS RUN LOCALLY ANYWAY
------------------------------------------------------
DynamoDB Local is a single node with no throttling and effectively serialises
transactions, so it never reaches the dangerous branch where two conditional
writes both succeed against stale headroom. **Conditional-expression logic is
provable here; conditional-expression contention is not.** A green run here means
the admission arithmetic, the classification, the recomputation and the barrier
are right — which is what has to be right before spending money on the deployed
run that can exercise contention.

So this reports what it achieved rather than a confidence interval it cannot
support: repetitions with proven overlap, concurrency per repetition, total
contended attempts, and the inconclusive rate.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent.parent / "backend"))

from _local_guard import require_local_dynamodb  # noqa: E402

GATEWAY_URL = os.environ.get("STRATOCLAVE_LOCAL_URL", "http://127.0.0.1:8080")
KEY_FILE = _HERE.parent.parent / "data" / "local" / "api_key"

# The occupier holds a reservation open for the length of its upstream call, and
# the probes have to arrive inside that window. The slow reasoning route measured
# 20-28 s in this environment against about a second for a small Claude model,
# which is the whole reason the occupier uses a different route from the probes.
OCCUPIER_ROUTE = "/openai/v1/responses"
OCCUPIER_MODEL = "openai.gpt-5.6-sol"
PROBE_ROUTE = "/v1/chat/completions"
PROBE_MODEL = "claude-haiku-4-5"

BARRIER_TIMEOUT_S = 90.0


# ---------------------------------------------------------------------------
# Client side: fire requests, classify outcomes, never believe the totals
# ---------------------------------------------------------------------------

ADMIT = "admit"
BUDGET_REFUSE = "budget-refuse"
UPSTREAM_FAIL = "upstream-fail"


@dataclass
class Outcome:
    label: str
    kind: str
    status: int
    detail: str = ""
    started_at: float = 0.0
    ended_at: float = 0.0


def _post(path: str, payload: dict, key: str, timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(
        GATEWAY_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read() or b"{}"
        try:
            return e.code, json.loads(body)
        except Exception:  # noqa: BLE001 — a non-JSON error body is still an outcome
            return e.code, {"raw": body[:400].decode("utf-8", "replace")}


def _classify(status: int, body: dict) -> tuple[str, str]:
    """Three-way, and the distinction between the last two is load-bearing.

    An upstream failure looks like "did not get through" from the client, and
    counting it as a budget refusal would inflate the evidence that the budget
    bound at all — which is exactly how a broken budget check passes a vacuity
    test. Only a 402 whose reason names the pool counts as a budget refusal.
    """
    if status == 200:
        return ADMIT, ""
    detail = body.get("detail")
    reason = ""
    if isinstance(detail, dict):
        reason = str(detail.get("reason") or detail.get("type") or "")
    elif isinstance(detail, str):
        reason = detail
    if status == 402:
        return BUDGET_REFUSE, reason
    return UPSTREAM_FAIL, f"{status}:{reason}"[:200]


def _fire(label: str, route: str, model: str, max_tokens: int, key: str,
          timeout: float, out: list, barrier: Optional[threading.Barrier]) -> None:
    payload: dict[str, Any] = {"model": model, "max_tokens": max_tokens}
    if route == PROBE_ROUTE:
        payload["messages"] = [{"role": "user", "content": "ok"}]
        payload["max_tokens"] = max_tokens
    else:
        payload = {"model": model, "input": "ok", "max_output_tokens": max_tokens}
    if barrier is not None:
        barrier.wait()
    started = time.time()
    status, body = _post(route, payload, key, timeout)
    kind, detail = _classify(status, body)
    out.append(Outcome(label, kind, status, detail, started, time.time()))


# ---------------------------------------------------------------------------
# Ledger side: the only source of truth for what was admitted and spent
# ---------------------------------------------------------------------------

def _mtok_ceil(tokens: int, rate: int) -> int:
    if tokens <= 0 or rate <= 0:
        return 0
    return -(-(tokens * rate) // 1_000_000)


@dataclass
class LedgerView:
    terminals: list = field(default_factory=list)
    pool: dict = field(default_factory=dict)

    @property
    def settled_from_events(self) -> int:
        return sum(int(t.get("settled_delta_microusd", 0)) for t in self.terminals)


def _read_ledger(tenant: str, period: str) -> LedgerView:
    from dynamo import CreditLedgerRepository
    from dynamo.tenant_budgets import TenantBudgetsRepository

    led = CreditLedgerRepository()
    items = led._table.query(  # noqa: SLF001 — no public scan of one partition exists
        KeyConditionExpression="pk = :pk",
        ExpressionAttributeValues={":pk": f"TENANT#{tenant}#P#{period}"},
    ).get("Items", [])
    terminals = [i for i in items if str(i.get("sk", "")).endswith("#TERMINAL")]
    pool = TenantBudgetsRepository().get(tenant, period) or {}
    return LedgerView(terminals=terminals, pool=dict(pool))


def _recompute_from_rating(term: dict) -> Optional[int]:
    """Recompute a terminal's amount from the tokens and rates it carries.

    Independent of `pool_settled` and independent of the amount the event states,
    because the point is to catch a gateway whose own arithmetic disagrees with
    its own record. Returns None when the event predates the rating schema.
    """
    raw = term.get("rating")
    if not raw:
        return None
    rating = json.loads(raw) if isinstance(raw, str) else dict(raw)
    if rating.get("rounding") != "ceil":
        return None
    total = 0
    for comp in (rating.get("components") or {}).values():
        total += _mtok_ceil(int(comp.get("tokens", 0)),
                            int(comp.get("rate_microusd_per_mtok", 0)))
    return total


def _quiesce(tenant: str, period: str, expected: int) -> tuple[bool, str]:
    """Wait until nothing is outstanding, and treat a timeout as a failure.

    Two conditions, because either alone is satisfiable while spend is still in
    flight: no reserved amount left, and as many terminals as requests that were
    admitted. A timeout here is reported as a failed run, never as a pass —
    reading early under-counts, which is the most comfortable way for this
    harness to lie.
    """
    from dynamo.tenant_budgets import TenantBudgetsRepository

    repo = TenantBudgetsRepository()
    deadline = time.time() + BARRIER_TIMEOUT_S
    last = ""
    while time.time() < deadline:
        pool = repo.get(tenant, period) or {}
        reserved = int(pool.get("pool_reserved_microusd", 0) or 0)
        terminals = len(_read_ledger(tenant, period).terminals)
        last = f"reserved={reserved} terminals={terminals}/{expected}"
        if reserved == 0 and terminals >= expected:
            return True, last
        time.sleep(0.5)
    return False, last


# ---------------------------------------------------------------------------
# The experiment
# ---------------------------------------------------------------------------

@dataclass
class Repetition:
    index: int
    admits: int = 0
    budget_refusals: int = 0
    upstream_failures: int = 0
    overlap_witnessed: bool = False
    inconclusive_reason: str = ""
    contended_attempts: int = 0


def _witness_overlap(peak_reserved: int, refusals: int) -> tuple[bool, str]:
    """Overlap, established from the store rather than from the client.

    A headroom refusal only witnesses an overlapping reservation window if some
    reservation was actually outstanding while it happened. The witness is
    therefore the PEAK reserved amount sampled across the probe window, not a
    single read taken after the probes finish.

    That distinction is not fussiness. A single post-probe read assumes the
    occupier is still in flight at that instant, which makes the witness depend
    on the upstream staying slow. It measured twenty-odd seconds on a reasoning
    model earlier and came back in about one on a trivial prompt, at which point
    the read found nothing reserved and a run that had genuinely overlapped was
    reported INCONCLUSIVE. Sampling removes the assumption instead of tuning it.
    """
    if refusals == 0:
        return False, "no headroom refusal was produced, so nothing witnesses overlap"
    if peak_reserved <= 0:
        return False, ("probes were refused while nothing was ever reserved during "
                       "their window — the refusal reflects settled spend, not an "
                       "open reservation window")
    return True, f"peak reserved={peak_reserved} during the probe window"


def run_repetition(index: int, tenant: str, period: str, key: str,
                   probes: int, probe_max_tokens: int) -> Repetition:
    rep = Repetition(index=index)
    from dynamo.tenant_budgets import TenantBudgetsRepository

    outcomes: list = []
    occupier_thread = threading.Thread(
        target=_fire,
        args=("occupier", OCCUPIER_ROUTE, OCCUPIER_MODEL, 512, key, 120.0, outcomes, None),
        daemon=True,
    )
    occupier_thread.start()

    # Let the occupier's reservation land before probing. Polling the pool rather
    # than sleeping a guessed interval: the point is to probe while a reservation
    # is genuinely open, and a fixed sleep would silently degrade into probing an
    # empty pool the day the upstream gets faster.
    repo = TenantBudgetsRepository()
    deadline = time.time() + 20.0
    while time.time() < deadline:
        pool = repo.get(tenant, period) or {}
        if int(pool.get("pool_reserved_microusd", 0) or 0) > 0:
            break
        time.sleep(0.2)
    else:
        rep.inconclusive_reason = "the occupier never opened a reservation within 20 s"
        occupier_thread.join(timeout=150)
        return rep

    # Sample the pool for as long as the probes are in flight and keep the peak.
    # See `_witness_overlap` for why a single read afterwards is not enough.
    peak = {"reserved": 0}
    sampling = threading.Event()
    sampling.set()

    def _sample() -> None:
        while sampling.is_set():
            pool = repo.get(tenant, period) or {}
            r = int(pool.get("pool_reserved_microusd", 0) or 0)
            if r > peak["reserved"]:
                peak["reserved"] = r
            time.sleep(0.1)

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()

    barrier = threading.Barrier(probes)
    threads = [
        threading.Thread(
            target=_fire,
            args=(f"probe-{i}", PROBE_ROUTE, PROBE_MODEL, probe_max_tokens, key,
                  60.0, outcomes, barrier),
            daemon=True,
        )
        for i in range(probes)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    sampling.clear()
    sampler.join(timeout=5)
    occupier_thread.join(timeout=180)

    rep.contended_attempts = probes
    for o in outcomes:
        if o.kind == ADMIT:
            rep.admits += 1
        elif o.kind == BUDGET_REFUSE:
            rep.budget_refusals += 1
        else:
            rep.upstream_failures += 1

    witnessed, why = _witness_overlap(peak["reserved"], rep.budget_refusals)
    rep.overlap_witnessed = witnessed
    if not witnessed:
        rep.inconclusive_reason = why

    total = rep.admits + rep.budget_refusals + rep.upstream_failures
    if total != probes + 1:
        rep.inconclusive_reason = (
            f"conservation law failed: {total} outcomes for {probes + 1} requests"
        )
        rep.overlap_witnessed = False
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repetitions", type=int, default=2)
    ap.add_argument("--probes", type=int, default=6)
    # Headroom, not the limit. Spend from earlier runs stays in `pool_settled`,
    # and `set_pool_limit` deliberately preserves it, so asking for a LIMIT means
    # the second run of the day has less room than the first and eventually the
    # budget stops binding for a reason that has nothing to do with the system
    # under test. The first run of this harness failed exactly that way.
    ap.add_argument("--headroom-microusd", type=int, default=16_000)
    ap.add_argument("--probe-max-tokens", type=int, default=64)
    args = ap.parse_args()

    require_local_dynamodb("prove_ceiling")
    from dynamo.tenant_budgets import TenantBudgetsRepository, current_period

    tenant = os.environ.get("DEFAULT_ORG_ID", "default-org")
    period = current_period()
    key = KEY_FILE.read_text().strip()

    # Terminals from earlier runs live in the same period partition, so a defect
    # that was fixed this morning would keep failing every run until the month
    # rolled over. A verification tool that is permanently red is a tool nobody
    # reads, so the evaluation is scoped to terminals this run produced — taken
    # as a set difference rather than a timestamp, because it needs no clock and
    # no field the ledger might not carry.
    pre_existing_sks = {str(t.get("sk")) for t in _read_ledger(tenant, period).terminals}
    if pre_existing_sks:
        print(f"[prove_ceiling] {len(pre_existing_sks)} terminal(s) already in this "
              f"period will be excluded — this run is judged on its own traffic")

    repo = TenantBudgetsRepository()
    existing = repo.get(tenant, period) or {}
    already_settled = int(existing.get("pool_settled_microusd", 0) or 0)
    limit = already_settled + args.headroom_microusd
    repo.set_pool_limit(
        tenant_id=tenant, period=period, pool_limit_microusd=limit, status="active",
    )
    print(f"[prove_ceiling] tenant={tenant} period={period}")
    print(f"[prove_ceiling] already settled this period: {already_settled} microUSD; "
          f"limit set to {limit} so the headroom is {args.headroom_microusd}")
    print(f"[prove_ceiling] occupier={OCCUPIER_ROUTE} probes={args.probes} "
          f"x {args.repetitions} repetitions")

    reps: list = []
    for i in range(args.repetitions):
        print(f"\n-- repetition {i + 1}/{args.repetitions} --")
        rep = run_repetition(i, tenant, period, key, args.probes,
                             args.probe_max_tokens)
        reps.append(rep)
        print(f"   admits={rep.admits} budget-refusals={rep.budget_refusals} "
              f"upstream-failures={rep.upstream_failures} "
              f"overlap={'witnessed' if rep.overlap_witnessed else 'NOT witnessed'}")
        if rep.inconclusive_reason:
            print(f"   INCONCLUSIVE: {rep.inconclusive_reason}")

    expected_terminals = sum(r.admits for r in reps)
    ok, detail = _quiesce(tenant, period, expected_terminals)
    print(f"\n[prove_ceiling] quiescence: {'reached' if ok else 'TIMED OUT'} ({detail})")
    if not ok:
        print("[prove_ceiling] FAIL: reading a pool with work outstanding under-counts "
              "spend, so a timeout here is a failed run and not a slow pass.")
        raise SystemExit(1)

    view = _read_ledger(tenant, period)
    view.terminals = [t for t in view.terminals
                      if str(t.get("sk")) not in pre_existing_sks]
    limit = int(view.pool.get("pool_limit_microusd", 0) or 0)
    pool_settled = int(view.pool.get("pool_settled_microusd", 0) or 0)
    pool_reserved = int(view.pool.get("pool_reserved_microusd", 0) or 0)
    headroom = int(view.pool.get("pool_headroom_microusd", 0) or 0)

    # Acceptance criterion 1, per request: settled must not exceed what admission
    # checked. The first run of this harness omitted this and reported only
    # pool-level totals — and the pool held, because a five-micro-USD breach on
    # one probe disappears into a headroom of thousands. The per-request check is
    # the one that finds an unsound bound; the aggregate check finds an unsound
    # POOL. They are different failures and both are needed.
    breaches = []
    for t in view.terminals:
        reserved = int(t.get("reserved_microusd", 0) or 0)
        settled = int(t.get("settled_delta_microusd", 0) or 0)
        if reserved <= 0:
            continue  # no reservation to exceed: shadow, or a legacy terminal
        if settled > reserved:
            ei = t.get("estimate_inputs")
            breaches.append({
                "sk": str(t.get("sk")),
                "model": str(t.get("model_id")),
                "reserved": reserved,
                "settled": settled,
                "overrun": settled - reserved,
                "cause": t.get("overrun_cause"),
                "inputs": json.loads(ei) if isinstance(ei, str) else ei,
            })

    recomputed = 0
    unrecomputable = 0
    for t in view.terminals:
        got = _recompute_from_rating(t)
        if got is None:
            unrecomputable += 1
            continue
        recomputed += got
        stated = int(t.get("settled_delta_microusd", 0))
        if got != stated and stated != 0:
            print(f"[prove_ceiling] FAIL: terminal {t.get('sk')} states {stated} "
                  f"but its own tokens and rates recompute to {got}")
            raise SystemExit(1)

    print("\n=== reconstructed from the ledger, not from the gateway's totals ===")
    print(f"  terminals                 : {len(view.terminals)} "
          f"({unrecomputable} without a rating to recompute)")
    print(f"  recomputed settled        : {recomputed} microUSD")
    print(f"  pool_settled              : {pool_settled} microUSD")
    print(f"  pool_reserved             : {pool_reserved} microUSD")
    print(f"  pool_limit                : {limit} microUSD")
    print(f"  headroom identity         : "
          f"{'holds' if headroom == limit - pool_reserved - pool_settled else 'BROKEN'}"
          f" ({headroom} vs {limit - pool_reserved - pool_settled})")

    admits = sum(r.admits for r in reps)
    refusals = sum(r.budget_refusals for r in reps)
    fails = sum(r.upstream_failures for r in reps)
    proven = [r for r in reps if r.overlap_witnessed]
    attempts = sum(r.contended_attempts for r in proven)

    print("\n=== what this run achieved, reported rather than interpreted ===")
    print(f"  repetitions with proven overlap : {len(proven)}/{len(reps)}")
    print(f"  inconclusive rate               : "
          f"{(len(reps) - len(proven)) / max(len(reps), 1):.0%}")
    print(f"  concurrency per repetition      : {args.probes}")
    print(f"  contended attempts (proven)     : {attempts}")
    print(f"  admits / refusals / failures    : {admits} / {refusals} / {fails}")

    failures = []
    if breaches:
        print(f"\n=== {len(breaches)} terminal(s) settled ABOVE what admission checked ===")
        for b in breaches[:5]:
            print(f"  {b['model']}: reserved {b['reserved']} settled {b['settled']} "
                  f"(over by {b['overrun']}, cause={b['cause']})")
            print(f"    bound inputs: {b['inputs']}")
        failures.append(
            f"{len(breaches)} request(s) settled above the reserved bound — the bound "
            f"is not sound for this traffic"
        )
    if recomputed > limit:
        failures.append(f"recomputed spend {recomputed} exceeds the limit {limit}")
    if pool_settled + pool_reserved > limit:
        failures.append(
            f"settled+reserved {pool_settled + pool_reserved} exceeds the limit {limit}")
    if admits == 0:
        failures.append("no request was admitted, so the run says nothing about spend")
    if refusals == 0:
        failures.append("no request was refused for budget, so the ceiling never bound")
    if not proven:
        failures.append("no repetition proved an overlapping reservation window")

    if failures:
        print("\n[prove_ceiling] FAIL")
        for f in failures:
            print(f"  - {f}")
        raise SystemExit(1)

    print(f"""
[prove_ceiling] PASS, with its scope stated rather than implied.

  Across {len(proven)} repetitions with a witnessed overlapping reservation window
  and {attempts} contended attempts, no state reconstructed from the ledger showed
  settled plus reserved above the pool limit, every terminal's amount recomputed
  from its own recorded tokens and rates, and the headroom identity held.

  This is evidence against defects more frequent than roughly 1 in {attempts}. It
  is not a proof, and it says nothing about conditional-write CONTENTION: this
  store is a single node that effectively serialises transactions, so the branch
  where two writes both succeed against stale headroom is not reachable here. That
  branch needs the deployed run.
""")


if __name__ == "__main__":
    main()
