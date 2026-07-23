<!-- Last updated: 2026-07-22 -->

# Demos

Courtesy samples that show, on inspectable data, what Stratoclave does that a
passthrough AI gateway structurally cannot. These are **not** benchmarks of your
workload and **not** audited tenant numbers — every sample is stamped
`SYNTHETIC` and is reproducible so you can mutate the inputs yourself.

## Savings Certificate

The counterfactual "if you'd followed the router's advice, how much cheaper — or
dearer — would this exact workload have been?", priced apples-to-apples at one
rate snapshot, escalation loss subtracted (not hidden), reproducible, and honest
about what it has **not** measured (quality).

| Sample | How it runs | Read |
|---|---|---|
| **Offline** (recommended first read) | One command, no cloud, no network — the real engine over a checked-in 9-request workload | [`savings-vs-litellm.md`](savings-vs-litellm.md) — side-by-side with a passthrough spend log, plus the four things a spend log cannot produce |
| **Live** | The real engine on live `scverify` DynamoDB (EC2, seeded traffic) | [`savings-certificate-sample.md`](savings-certificate-sample.md) |

Reproduce the offline one:

```bash
cd backend
python ../bench/savings/demo_offline.py            # human-readable certificate
python ../bench/savings/demo_offline.py --detail   # + per-request counterfactual
python ../bench/savings/demo_offline.py --json      # raw JSON
```

Workload: [`bench/savings/demo_workload.jsonl`](../../bench/savings/demo_workload.jsonl)
(rows carry no cost — the demo recomputes each bill with the shipped pricer, so
nothing flattering is hand-authored). The figures in the doc are pinned to the
engine's output by `backend/tests/test_savings_demo_offline.py`, so they cannot
silently drift.

Engine and design: [`../design/vsr-savings-certificate.md`](../design/vsr-savings-certificate.md).
