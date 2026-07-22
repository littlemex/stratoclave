<!-- Last updated: 2026-07-22 -->

# Workshops

Hands-on scenarios you run to see what Stratoclave does across three axes —
**cost, performance, accuracy** — and, just as honestly, what it does *not* do
yet. Each scenario is a walkthrough you follow, deterministic and offline-first;
where a step cannot run on shipped code, it says so and points at the gap rather
than faking a number.

The gaps are the point. Running a workshop turns "I want to measure X" into a
machine-checked entry in [`COVERAGE.md`](COVERAGE.md) — a demand-driven list of
the next capabilities to build (see [`GAPS.md`](GAPS.md)).

> These are courtesy samples on synthetic data, **not** benchmarks of your
> workload and **not** audited numbers. Measurement, evaluation, and acceptance
> bars are your responsibility; the workshop provides the mechanism.

## How this relates to `docs/` and `bench/`

- **`bench/`** is machinery that reproduces numbers (load, latency, savings).
- **`docs/`** explains the system.
- **`scenarios/`** (here) is a walkthrough a person follows, wiring shipped
  commands and `bench/` scripts into a guided experience — and recording, per
  step, what is `covered` vs a gap. Scenarios may call `bench/`; nothing depends
  back on `scenarios/`.

## Scenarios

| Path | Audience | Axes | Read |
|---|---|---|---|
| [`usage/small-team`](usage/small-team/scenario.md) ([日本語](usage/small-team/scenario_ja.md)) | user | cost · perf · quality — offline + real-Bedrock **through the gateway** (charge-of-record, paired overhead) + a direct baseline | A three-person team on one shared budget pool |

Planned (folders to come, same four-file shape): `admin/setup-tenant` (build-out),
more `usage/*`, `perf/*`, `quality/*`.

## Anatomy of a scenario

Each scenario directory ships four files (see [`_schema/`](_schema/)):

- `scenario.md` + `scenario_ja.md` — the same walkthrough in English and Japanese.
- `run.py` — the runnable, deterministic part (offline-first; pure folds over
  checked-in data so figures can be CI-pinned).
- `coverage.yaml` — machine-readable per-step state: `covered`,
  `covered-elsewhere`, `not-implemented` (needs an issue link), or
  `user-responsibility`.

`backend/tests/test_scenarios_coverage.py` aggregates every `coverage.yaml` into
[`COVERAGE.md`](COVERAGE.md) and enforces two rules: a gap must carry a tracking
issue, and a step that claims to *measure* something must actually run on shipped
code.
