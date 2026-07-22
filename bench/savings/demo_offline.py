"""OFFLINE, one-command VSR Savings Certificate demo — no DynamoDB, no network.

The companion to `demo_certificate_seed.py` (which runs the REAL engine on LIVE
scverify infra). This one drives the SAME real engine — `mvp.learning.savings`
folded with the SHIPPED `_default_pricer` / `_default_resolver` (the built-in
`_DEFAULT_RATES`) — over a tiny, checked-in, deterministic workload
(`demo_workload.jsonl`), so anyone can reproduce the exact number with:

    python -m bench.savings.demo_offline            # from backend/

WHY IT EXISTS. The whole product claim is a number litellm structurally cannot
produce: for each VSR-acted request, "if the tenant had FOLLOWED the routing
advice, how much cheaper — or DEARER — would this exact workload have been?",
priced apples-to-apples at ONE rate snapshot over the SAME token counts, with the
bias forced to the conservative (VSR-unfavourable) side. This demo shows that
number, honestly, on data a reader can inspect and mutate.

HONESTY (enforced here, not just documented):
  * The workload rows carry NO cost — this script recomputes `cost_microusd` with
    the REAL pricer, so the demo cannot smuggle in a flattering hand-picked bill.
  * The workload deliberately includes an ESCALATION LOSS row (VSR advised the
    dearer model) so `net` is dragged DOWN, not cherry-picked upward.
  * SHADOW-advised rows are POTENTIAL only and never enter the realized headline.
  * `quality.measured` stays False — Stratoclave proves the COST counterfactual
    but refuses to claim quality without a tenant's own eval. That refusal is
    itself the point (see docs/demo/README.md).
  * The certificate is stamped `traffic: synthetic` and the CLI prints a loud
    banner, so this can never be mistaken for an audited tenant number.

NOTHING is written anywhere; this is a pure fold. Engine code is UNCHANGED — this
is glue over the shipped `summarize_savings(rows, price=, resolve=)` seam.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure `backend/` is importable whether run as a module or a script.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if _BACKEND.is_dir() and str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from mvp.learning import savings as sv          # noqa: E402
from mvp.learning import savings_cli as cli     # noqa: E402
from mvp.pricing import BUILTIN_VERSION         # noqa: E402

_WORKLOAD = Path(__file__).with_name("demo_workload.jsonl")


def load_rows(path: Path = _WORKLOAD) -> list[dict]:
    """Read the checked-in workload. Each line is a reconcile-join row MINUS the
    cost, which we fill from the real pricer so the bill is not hand-authored."""
    rows: list[dict] = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def price_rows(rows: list[dict], price, resolve) -> list[dict]:
    """Fill `cost_microusd` = the REAL charge of the BILLED model over the row's
    tokens (built-in default rates). A row whose billed model is unpriceable keeps
    cost_microusd absent (the engine classes it honestly, never as a fake 0)."""
    out = []
    for r in rows:
        r = dict(r)
        billed = resolve(str(r.get("billed_model_id") or ""))
        tin, tout = r.get("input_tokens"), r.get("output_tokens")
        if billed and tin is not None and tout is not None:
            r["cost_microusd"] = int(price(billed["pricing_key"], int(tin), int(tout)))
        # the `note` key is ignored by the engine (it reads only known fields).
        out.append(r)
    return out


def build_certificate() -> dict:
    """Assemble the SAME certificate shape `savings_certificate` returns, but from
    the offline workload — so the shipped CLI formatter renders it unchanged."""
    price = sv._default_pricer()
    resolve = sv._default_resolver()
    rows = price_rows(load_rows(), price, resolve)
    savings = sv.summarize_savings(rows, price=price, resolve=resolve)
    return {
        "tenant_id": "demo-offline",
        "day": "(offline seed)",
        "traffic": "synthetic",
        "rate_version": BUILTIN_VERSION,
        "savings": savings,
        "reconcile": {"source": "bench/savings/demo_workload.jsonl (checked-in)"},
    }


def main(argv: list[str] | None = None) -> int:
    cert = build_certificate()
    as_json = "--json" in (argv or sys.argv[1:])
    with_detail = "--detail" in (argv or sys.argv[1:])

    if as_json:
        print(json.dumps(cert, indent=2, sort_keys=True, default=str))
        return 0

    # Render through the SHIPPED CLI formatter by pinning its fetch to our cert.
    _orig = sv.savings_certificate
    sv.savings_certificate = lambda **kw: cert  # type: ignore[assignment]
    try:
        args = ["--tenant", cert["tenant_id"], "--day", "offline",
                "--traffic", "synthetic"]
        if with_detail:
            args.append("--detail")
        cli.main(args)
    finally:
        sv.savings_certificate = _orig

    # Reproducibility proof: recompute and assert byte-identical headline.
    again = build_certificate()
    same = (again["savings"]["net_saving_microusd"]
            == cert["savings"]["net_saving_microusd"])
    print(f"\n[reproducible] re-running the fold yields the identical net "
          f"({cert['savings']['net_saving_microusd']} micro-USD): {same}")
    return 0 if same else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
