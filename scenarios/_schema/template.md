<!-- Template for a Stratoclave workshop scenario. Copy this file to
     scenarios/<axis-or-audience>/<name>/scenario.md and its _ja.md pair.
     Every scenario ships FOUR files: scenario.md, scenario_ja.md, run.py
     (the runnable, deterministic part — offline-first), coverage.yaml. -->

# <Scenario title>

**Audience:** admin | user
**Runs on:** shipped, all-green artifacts only (offline-first) — anything that
does not is declared a gap in `coverage.yaml`, never faked.

## What you'll see

One paragraph: the concrete thing a reader walks away having done, and which of
the three axes (cost / performance / accuracy) it touches. Name up front which
steps run today and which deliberately hit a gap — the gap is part of the lesson.

## Prerequisites

- Environment (offline vs live; which shipped commands/tests).
- **Responsibility boundary (state it plainly):** measurement, evaluation,
  availability targets, and backend fit are the *operator's* responsibility.
  This scenario provides the *mechanism* (a deterministic script, a metric
  definition, a pure scoring fold), not an audited result for your workload.

## Steps

Numbered, copy-pasteable. Each step maps to one entry in `coverage.yaml`. For a
step that a reader can run now, show the command and the expected shape of the
output. For a step that hits a gap, say so explicitly and point at the tracking
issue — "try to measure X; today the gateway only surfaces Y; that gap is
issue #NNN."

## Measurement

What is measured, and by which mechanism. If a metric is not yet emitted by the
gateway, this section states that the metric is *defined here as a template* and
the emission is a gap — it does not invent a number.

## Expected result

The deterministic outcome (pinned in `run.py` / a CI test where numbers appear in
prose, so the doc can never silently drift — same posture as the Savings demo).

## Coverage & gaps

A prose summary of what `coverage.yaml` encodes: which steps are `covered`,
which are `not-implemented` (with issue links), which are `user-responsibility`.
The aggregated view across all scenarios lives in the auto-generated
`scenarios/COVERAGE.md`.
