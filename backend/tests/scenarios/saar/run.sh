#!/usr/bin/env bash
# Exhaustive SAAR blog-scenario verification — the single entry point meant to
# run every commit (pure + deterministic, no AWS). It (1) runs the nine-scenario
# coverage suite and (2) emits the metrics report as one JSON line so a CI/cron
# run leaves an auditable, greppable record.
#
#   backend/tests/scenarios/saar/run.sh
#
# Exit non-zero if any scenario assertion fails OR the provider-state xfail
# unexpectedly passes (strict xfail) — i.e. the catalogue drifted from reality.
set -euo pipefail

# Resolve to the backend/ root regardless of caller cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$(cd "$HERE/../../.." && pwd)"
cd "$BACKEND"

echo "=== SAAR blog-scenario coverage (pytest -m saar_scenario) ==="
python3 -m pytest -m saar_scenario -q -p no:cacheprovider

echo ""
echo "=== SAAR metrics reproduction (deterministic; one JSON line) ==="
python3 -m tests.scenarios.saar.metrics
