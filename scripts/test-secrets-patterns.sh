#!/usr/bin/env bash
# Regression guard for sweep-4 Critical: the secrets pre-commit scanner
# must carry ALL of the sweep-2 X-4 pattern labels. If a future server
# squash drops any of them, this script exits non-zero and the CI job
# that calls it fails LOUDLY.
#
# Runs a static grep over check-no-hardcoded-secrets.sh, not an actual
# scan — we only need to assert the pattern registry is intact.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${HERE}/check-no-hardcoded-secrets.sh"

if [[ ! -f "$TARGET" ]]; then
  echo "[FAIL] ${TARGET} missing" >&2
  exit 1
fi

declare -a REQUIRED=(
  "cloudfront-distribution"
  "cognito-user-pool"
  "alb-dns"
  "ecr-uri"
  "aws-access-key"
  "jwt-token"
  "aws-secret-context"
  # sweep-2 X-4 additions — the three that kept vanishing on squash:
  "stratoclave-plaintext-apikey"
  "bare-aws-account-id"
  "cognito-refresh-token"
)

fail=0
for label in "${REQUIRED[@]}"; do
  if ! grep -q "\"${label}|" "$TARGET"; then
    echo "[FAIL] missing required secret-scan label: ${label}" >&2
    fail=1
  fi
done

if [[ $fail -ne 0 ]]; then
  echo "" >&2
  echo "Edit scripts/check-no-hardcoded-secrets.sh and reintroduce the missing" >&2
  echo "pattern(s). Do not remove this test — it catches server-side squash" >&2
  echo "drops and silent allowlist regressions." >&2
  exit 1
fi

echo "[OK] all required secret-scan patterns present"
