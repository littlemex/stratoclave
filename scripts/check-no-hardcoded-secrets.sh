#!/usr/bin/env bash
# check-no-hardcoded-secrets.sh — scan the tree for hard-coded deployment
# identifiers that should live in IaC output / env vars / examples instead.
#
# Purpose: prevent secrets and deployment-specific values (CloudFront
# distribution IDs, account IDs, Cognito pool IDs, ALB DNS names,
# passwords, JWTs, AWS keys) from being committed to docs, blog posts,
# or source code. Runs on pull_request and push via .github/workflows/.
#
# Matches patterns by SHAPE, not fixed strings, so it keeps working as
# deployments are rotated.
#
# Exit codes:
#   0  clean
#   1  one or more hits found (CI should fail)
#
# Tune ALLOWLIST_REGEX / IGNORE_PATHS below to reduce false positives.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ----------------------------------------------------------------------
# Paths that are either generated or known-safe-to-contain samples.
# ----------------------------------------------------------------------
IGNORE_PATHS=(
  ":(exclude).git"
  ":(exclude)**/node_modules/**"
  ":(exclude)**/target/**"
  ":(exclude)**/dist/**"
  ":(exclude)**/cdk.out/**"
  ":(exclude)**/*.lock"
  ":(exclude)**/package-lock.json"
  ":(exclude)**/Cargo.lock"
  ":(exclude)**/cdk.context.json"
  # Generated / artefact docs
  ":(exclude)docs/screenshots/**"
  ":(exclude)docs/diagrams/**/*.png"
  ":(exclude)docs/diagrams/**/*.drawio"
  # This script itself carries example patterns as comments/tests.
  ":(exclude)scripts/check-no-hardcoded-secrets.sh"
)

# ----------------------------------------------------------------------
# Allowlist: anchor names that are intentionally in source (e.g. example
# labels, third-party well-known IDs).
# ----------------------------------------------------------------------
ALLOWLIST_REGEX='<your-|<account-id>|<pool-id>|<client-id>|<subdomain>|xxxx|example\.com|placeholder|EXAMPLE|YOUR-DEPLOYMENT|us-east-1_XXXXX|test-alb-|fake-|dummy-|d111111abcdef8|d1234\.cloudfront\.net'

# ----------------------------------------------------------------------
# Pattern definitions.
# Each line: "LABEL<TAB>regex"
# Regex is POSIX ERE passed to grep -E.
# ----------------------------------------------------------------------
PATTERNS=(
  # CloudFront distribution domains are <13-14 char lowercase base32ish>.cloudfront.net
  "cloudfront-distribution|[a-z0-9]{13,14}\.cloudfront\.net"

  # Cognito User Pool ID: <region>_<9 chars A-Za-z0-9>
  "cognito-user-pool|(us|eu|ap|sa|ca|af|me)-(east|west|north|south|central|northeast|southeast)-[0-9]_[A-Za-z0-9]{9}"

  # ALB DNS: <name>-<9-10 digit>.<region>.elb.amazonaws.com
  "alb-dns|[a-z0-9-]+-[0-9]{9,10}\.[a-z0-9-]+\.elb\.amazonaws\.com"

  # ECR URI with 12-digit account id: <12-digit>.dkr.ecr.<region>.amazonaws.com
  "ecr-uri|[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com"

  # AWS access key (live) prefixes
  "aws-access-key|(AKIA|ASIA)[A-Z0-9]{16}"

  # JWT (header.payload.signature, base64url). Only match very long tokens
  # to avoid false positives on short JWT-like strings in tests.
  "jwt-token|eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{30,}"

  # Raw AWS secret access key (40 char base64) — high false-positive risk;
  # require surrounding key-like context (aws_secret, SecretAccessKey).
  "aws-secret-context|(aws_secret_access_key|SecretAccessKey|AWS_SECRET)[ '\"=:]+[A-Za-z0-9/+=]{40}"
)

fail=0
found_report=()

# ----------------------------------------------------------------------
# Scan loop.
# ----------------------------------------------------------------------
for entry in "${PATTERNS[@]}"; do
  label="${entry%%|*}"
  pattern="${entry#*|}"

  # Scan tracked + untracked files (but not .gitignore'd ones) so new
  # leaks in files not yet `git add`'d are caught by pre-commit hook.
  # `git grep --untracked` covers both.
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    hits=$(git grep -nE --untracked "$pattern" -- "${IGNORE_PATHS[@]}" 2>/dev/null || true)
  else
    hits=$(grep -rnE "$pattern" . \
      --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=target \
      --exclude-dir=dist --exclude-dir=cdk.out --exclude-dir=screenshots \
      --exclude="*.lock" --exclude="package-lock.json" --exclude="Cargo.lock" \
      --exclude="cdk.context.json" \
      --exclude="$(basename "$0")" \
      2>/dev/null || true)
  fi

  # Filter out allowlisted lines.
  if [[ -n "$hits" ]]; then
    filtered=$(printf '%s\n' "$hits" | grep -Ev "$ALLOWLIST_REGEX" || true)
    if [[ -n "$filtered" ]]; then
      fail=1
      found_report+=("=== [$label] ===")
      found_report+=("$filtered")
      found_report+=("")
    fi
  fi
done

if [[ $fail -ne 0 ]]; then
  printf '[FAIL] Hard-coded deployment identifier(s) found:\n\n' >&2
  printf '%s\n' "${found_report[@]}" >&2
  printf '\nGuidance:\n' >&2
  printf '  - Replace CloudFront URLs with <your-deployment>.cloudfront.net\n' >&2
  printf '  - Replace account/pool/client IDs with <account-id>/<pool-id>/<client-id>\n' >&2
  printf '  - For ARN-like strings in docs, use arn:aws:service:region:<account-id>:...\n' >&2
  printf '  - If a match is a legitimate test fixture, extend ALLOWLIST_REGEX in\n' >&2
  printf '    scripts/check-no-hardcoded-secrets.sh rather than hard-coding the value.\n' >&2
  exit 1
fi

printf '[OK] no hard-coded deployment identifiers found.\n'
exit 0
