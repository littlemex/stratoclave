#!/usr/bin/env bash
# docs-e2e.sh
#
# Dry-run the commands that appear in docs/GETTING_STARTED.md +
# docs/DEPLOYMENT.md as a self-check. Builds the CLI, synthesizes the
# CDK tree, installs + tests the Backend, and issues a handful of live
# CLI/API probes against the currently deployed gateway. No AWS
# resources are created or destroyed.
#
# Usage:
#   AWS_PROFILE=claude-code STRATOCLAVE_API_ENDPOINT=https://<your>.cloudfront.net ./scripts/docs-e2e.sh
#
# Environment variables:
#   AWS_PROFILE                 required. Used for CDK synth / AWS SDK calls.
#   STRATOCLAVE_API_ENDPOINT    required. CloudFront URL of a live deployment.
#   CDK_DEFAULT_ACCOUNT         optional. Falls back to `aws sts get-caller-identity`.
#   SKIP_BACKEND_TEST=1         skip `pytest` (e.g. no Python 3.11+ available).
#   SKIP_LIVE_PROBES=1          skip CLI calls that need a logged-in session.
#   STRATOCLAVE_CLI             path override for the CLI binary.
#
set -euo pipefail

log()  { printf '\033[0;34m[%(%H:%M:%S)T]\033[0m %s\n' -1 "$*"; }
fail() { printf '\033[0;31m[%(%H:%M:%S)T] FAIL:\033[0m %s\n' -1 "$*" >&2; exit 1; }
ok()   { printf '\033[0;32m[%(%H:%M:%S)T] OK:\033[0m %s\n' -1 "$*"; }

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLI_DIR="$ROOT_DIR/cli"
BACKEND_DIR="$ROOT_DIR/backend"
IAC_DIR="$ROOT_DIR/iac"

: "${AWS_PROFILE:?AWS_PROFILE must be set (e.g. claude-code).}"
: "${STRATOCLAVE_API_ENDPOINT:?STRATOCLAVE_API_ENDPOINT must be set to the live CloudFront URL.}"

CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-us-east-1}"

log "Using AWS_PROFILE=$AWS_PROFILE account=$CDK_DEFAULT_ACCOUNT region=$CDK_DEFAULT_REGION endpoint=$STRATOCLAVE_API_ENDPOINT"

################################################################################
# 1. Toolchain sanity (from docs/DEPLOYMENT.md Prerequisites)
################################################################################
log "step 1: verifying local toolchain"
aws --version       >/dev/null 2>&1 || fail "aws cli not available"
node --version      >/dev/null 2>&1 || fail "node not available"
python3 --version   >/dev/null 2>&1 || fail "python3 not available"
cargo --version     >/dev/null 2>&1 || fail "cargo not available"
ok   "toolchain sanity"

################################################################################
# 2. CLI: cargo build --release (from docs/GETTING_STARTED.md)
################################################################################
log "step 2: cargo build --release"
pushd "$CLI_DIR" >/dev/null
cargo build --release --quiet
STRATOCLAVE_CLI="${STRATOCLAVE_CLI:-$CLI_DIR/target/release/stratoclave}"
test -x "$STRATOCLAVE_CLI" || fail "CLI binary not found at $STRATOCLAVE_CLI"
"$STRATOCLAVE_CLI" --help >/dev/null
popd >/dev/null
ok   "CLI built and --help runs"

################################################################################
# 3. IaC: cdk synth --all (from docs/DEPLOYMENT.md "Synth-time security checks")
################################################################################
log "step 3: cdk synth --all (cdk-nag enforced)"
pushd "$IAC_DIR" >/dev/null
if [ ! -d node_modules ]; then
  log "iac/node_modules missing; running npm install"
  npm install --silent
fi
JSII_SILENCE_WARNING_DEPRECATED_NODE_VERSION=1 \
  AWS_DEFAULT_REGION="$CDK_DEFAULT_REGION" \
  CDK_DEFAULT_REGION="$CDK_DEFAULT_REGION" \
  CDK_DEFAULT_ACCOUNT="$CDK_DEFAULT_ACCOUNT" \
  STRATOCLAVE_PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}" \
  ENVIRONMENT="${ENVIRONMENT:-development}" \
  CDK_NAG=on ENABLE_WAF=true \
  npx cdk synth --all --quiet >/dev/null
popd >/dev/null
ok   "cdk synth --all succeeded, cdk-nag enforced"

################################################################################
# 4. Backend: pytest (from docs/DEPLOYMENT.md Local development)
################################################################################
if [ "${SKIP_BACKEND_TEST:-}" = "1" ]; then
  log "step 4: SKIP_BACKEND_TEST=1 — skipping pytest"
else
  log "step 4: backend pytest"
  # Resolve a Python >= 3.11 interpreter. fastapi 0.136 and friends require
  # Python 3.10+; the system `python3` on recent macOS is 3.9 and is not
  # good enough. Users can force a specific one via $PYTHON.
  pick_python() {
    if [ -n "${PYTHON:-}" ]; then printf '%s' "$PYTHON"; return; fi
    for c in python3.12 python3.11 python3; do
      if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))' 2>/dev/null || echo 0.0)
        maj=${v%%.*}; min=${v##*.}
        if [ "$maj" = "3" ] && [ "$min" -ge 11 ] 2>/dev/null; then
          printf '%s' "$c"; return
        fi
      fi
    done
  }
  PY_BIN=$(pick_python)
  if [ -z "$PY_BIN" ]; then
    fail "no Python 3.11+ found on PATH (install via 'brew install python@3.12' or set \$PYTHON)"
  fi
  log "  using $PY_BIN ($($PY_BIN --version))"
  pushd "$BACKEND_DIR" >/dev/null
  VENV_DIR=".venv-docs-e2e"
  if [ ! -d "$VENV_DIR" ]; then
    "$PY_BIN" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install -q --upgrade pip
    "$VENV_DIR/bin/pip" install -q -r requirements-dev.txt
  fi
  "$VENV_DIR/bin/python" -m pytest -q tests/ >/dev/null
  popd >/dev/null
  ok   "backend pytest green"
fi

################################################################################
# 5. Live probes against the deployed gateway
################################################################################
log "step 5: live probes against $STRATOCLAVE_API_ENDPOINT"

log "  well-known discovery"
curl -fsS "${STRATOCLAVE_API_ENDPOINT%/}/.well-known/stratoclave-config" >/dev/null \
  || fail "/.well-known/stratoclave-config unreachable"
ok   "well-known reachable"

log "  security headers (HSTS / CSP / X-Frame)"
hdrs=$(curl -sI "${STRATOCLAVE_API_ENDPOINT%/}/")
echo "$hdrs" | grep -qi '^strict-transport-security:'    || fail "missing HSTS"
echo "$hdrs" | grep -qi '^content-security-policy:'      || fail "missing CSP"
echo "$hdrs" | grep -qi '^x-frame-options:'              || fail "missing X-Frame-Options"
echo "$hdrs" | grep -qi '^x-content-type-options: nosniff' || fail "missing X-Content-Type-Options"
ok   "security headers present"

log "  S3 origin is not directly reachable (OAC enforced)"
bucket=$(aws ssm get-parameter \
  --name "/${STRATOCLAVE_PREFIX:-stratoclave}/frontend/s3-bucket" \
  --query Parameter.Value --output text --region "$CDK_DEFAULT_REGION" 2>/dev/null || true)
if [ -n "$bucket" ] && [ "$bucket" != "None" ]; then
  status=$(curl -sS -o /dev/null -w '%{http_code}' "https://${bucket}.s3.${CDK_DEFAULT_REGION}.amazonaws.com/index.html")
  if [ "$status" = "403" ]; then
    ok "S3 direct access blocked (HTTP 403)"
  else
    fail "S3 direct access returned HTTP $status (expected 403)"
  fi
else
  log "  SSM param for bucket not found; skipping S3 probe"
fi

log "  ALB direct-IP probe (expect timeout / connection refused)"
alb_dns=$(aws elbv2 describe-load-balancers \
  --names "${STRATOCLAVE_PREFIX:-stratoclave}-alb" \
  --query 'LoadBalancers[0].DNSName' --output text --region "$CDK_DEFAULT_REGION" 2>/dev/null || true)
if [ -n "$alb_dns" ] && [ "$alb_dns" != "None" ]; then
  if curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "http://${alb_dns}/health" >/dev/null 2>&1; then
    fail "ALB direct probe succeeded — prefix-list restriction may be missing"
  else
    ok "ALB direct probe timed out as expected"
  fi
else
  log "  ALB not found; skipping direct-IP probe"
fi

################################################################################
# 6. Optional authenticated probes
################################################################################
if [ "${SKIP_LIVE_PROBES:-}" = "1" ]; then
  log "step 6: SKIP_LIVE_PROBES=1 — skipping authenticated probes"
  ok "docs-e2e complete"
  exit 0
fi

if [ ! -f "$HOME/.stratoclave/mvp_tokens.json" ]; then
  log "  no cached tokens — skipping authenticated probes. Run \`stratoclave auth sso\` first."
  ok "docs-e2e complete (unauthenticated leg)"
  exit 0
fi

log "step 6: authenticated CLI probes"
"$STRATOCLAVE_CLI" auth whoami >/dev/null   || fail "stratoclave auth whoami failed"
ok   "stratoclave auth whoami"

"$STRATOCLAVE_CLI" usage show >/dev/null    || fail "stratoclave usage show failed"
ok   "stratoclave usage show"

"$STRATOCLAVE_CLI" api-key list >/dev/null  || fail "stratoclave api-key list failed"
ok   "stratoclave api-key list"

# P0-8 regression guard: `stratoclave ui open / url` must not embed the
# access token in the URL. The sanctioned handoff channel is
# `?ui_ticket=<single-use-nonce>` minted by the backend. If this ever
# prints `?token=<jwt>` again the session-fixation primitive is back.
ui_url=$("$STRATOCLAVE_CLI" ui url 2>&1 | tail -n1)
if echo "$ui_url" | grep -qE 'token=eyJ|token=[A-Za-z0-9._\-]{40,}'; then
  fail "stratoclave ui url leaked an access_token in the URL (P0-8 regression): $ui_url"
fi
if ! echo "$ui_url" | grep -qE 'ui_ticket=stt_[A-Za-z0-9_\-]+'; then
  fail "stratoclave ui url did not embed a ui_ticket= handoff nonce: $ui_url"
fi
ok "stratoclave ui url uses ?ui_ticket= (no access_token in URL)"

# P0-8 consume round trip: freshly minted ticket should be redeemable
# exactly once and return a payload whose access_token round-trips
# through /v1/messages. Extracts the nonce from the URL, POSTs it to
# /ui-ticket/consume, then uses the returned access_token.
ticket=$(echo "$ui_url" | sed -E 's#.*ui_ticket=([^&]+).*#\1#')
consume=$(curl -sS -w '\n%{http_code}' \
  -X POST "${STRATOCLAVE_API_ENDPOINT%/}/api/mvp/auth/ui-ticket/consume" \
  -H 'Content-Type: application/json' \
  -A "stratoclave-docs-e2e/$(date +%s)" \
  -d "{\"ticket\":\"$ticket\"}")
consume_status=$(printf '%s' "$consume" | tail -n1)
if [ "$consume_status" != "200" ]; then
  fail "ui-ticket consume returned HTTP $consume_status"
fi
ok "ui-ticket consume returned 200"

# Replay MUST fail (single-use).
replay_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X POST "${STRATOCLAVE_API_ENDPOINT%/}/api/mvp/auth/ui-ticket/consume" \
  -H 'Content-Type: application/json' \
  -A "stratoclave-docs-e2e/$(date +%s)" \
  -d "{\"ticket\":\"$ticket\"}")
if [ "$replay_status" != "404" ]; then
  fail "ui-ticket replay returned HTTP $replay_status (expected 404)"
fi
ok "ui-ticket replay rejected (404)"

access=$(jq -r '.access_token' "$HOME/.stratoclave/mvp_tokens.json")
resp=$(curl -sS -w '\n%{http_code}' \
  -X POST "${STRATOCLAVE_API_ENDPOINT%/}/v1/messages" \
  -H "Authorization: Bearer $access" \
  -H 'Content-Type: application/json' \
  -A "stratoclave-docs-e2e/$(date +%s)" \
  -d '{"model":"claude-opus-4-7","max_tokens":5,"messages":[{"role":"user","content":"ping"}]}')
status=$(printf '%s' "$resp" | tail -n1)
body=$(printf '%s' "$resp" | sed '$d')
if [ "$status" != "200" ]; then
  fail "/v1/messages returned HTTP $status: $body"
fi
ok "/v1/messages returned 200"

# P1-B regression guard: the claude-wrapper API-key mint path must
# accept `ephemeral=true` + `expires_in_minutes=30`, bypass the active-
# key cap, and return a revokable key_id. We do the mint and revoke
# here directly so a future CLI rewrite that silently drops the
# wrapper-key mechanism trips this check.
mint_body='{"name":"stratoclave-claude-wrapper","scopes":["messages:send"],"ephemeral":true,"expires_in_minutes":5}'
mint=$(curl -sS -w '\n%{http_code}' \
  -X POST "${STRATOCLAVE_API_ENDPOINT%/}/api/mvp/me/api-keys" \
  -H "Authorization: Bearer $access" \
  -H 'Content-Type: application/json' \
  -A "stratoclave-docs-e2e/$(date +%s)" \
  -d "$mint_body")
mint_status=$(printf '%s' "$mint" | tail -n1)
mint_body_json=$(printf '%s' "$mint" | sed '$d')
if [ "$mint_status" != "201" ]; then
  fail "ephemeral wrapper-key mint returned HTTP $mint_status: $mint_body_json"
fi
wrapper_key_id=$(printf '%s' "$mint_body_json" | jq -r '.key_id')
if [ -z "$wrapper_key_id" ] || [ "$wrapper_key_id" = "null" ]; then
  fail "ephemeral wrapper-key response missing key_id"
fi
ok "ephemeral wrapper-key mint returned 201 (key_id=$wrapper_key_id)"

# P1-B regression guard: the revoke path must succeed and remove the
# key from the active listing.
revoke_status=$(curl -sS -o /dev/null -w '%{http_code}' \
  -X DELETE "${STRATOCLAVE_API_ENDPOINT%/}/api/mvp/me/api-keys/by-key-id/${wrapper_key_id}" \
  -H "Authorization: Bearer $access" \
  -A "stratoclave-docs-e2e/$(date +%s)")
if [ "$revoke_status" != "204" ]; then
  fail "ephemeral wrapper-key revoke returned HTTP $revoke_status"
fi
ok "ephemeral wrapper-key revoked (204)"

# P1-C regression guard: enableExecuteCommand must be OFF in production.
# We ask ECS describe-services directly (requires the caller's AWS
# credentials — this check is skipped when AWS_PROFILE is not set).
if [ -n "${AWS_PROFILE:-}" ]; then
  enable_exec=$(aws ecs describe-services \
    --cluster stratoclave-cluster \
    --services stratoclave-backend \
    --query 'services[0].enableExecuteCommand' \
    --output text 2>/dev/null || echo UNKNOWN)
  if [ "$enable_exec" = "False" ] || [ "$enable_exec" = "false" ]; then
    ok "ECS enableExecuteCommand=false on the backend service (P1-C)"
  elif [ "$enable_exec" = "UNKNOWN" ]; then
    log "  skipping ECS describe (no AWS access)"
  else
    fail "ECS backend service has enableExecuteCommand=$enable_exec — P1-C regression in production"
  fi
fi

# P1-A regression guard: an accidentally-open admin-bootstrap gate in
# production is one of the highest-impact foot-guns in the threat
# model. We probe it through the SSM parameter path the backend
# reads; the check is skipped when AWS access is not configured.
if [ -n "${AWS_PROFILE:-}" ]; then
  gate_env=$(aws ecs describe-task-definition \
    --task-definition stratoclave-backend \
    --query 'taskDefinition.containerDefinitions[0].environment' \
    --output json 2>/dev/null || echo '[]')
  allow_flag=$(printf '%s' "$gate_env" | jq -r '.[] | select(.name=="ALLOW_ADMIN_CREATION") | .value' | head -n1)
  allow_until=$(printf '%s' "$gate_env" | jq -r '.[] | select(.name=="ALLOW_ADMIN_CREATION_UNTIL") | .value' | head -n1)
  now_epoch=$(date -u +%s)
  if [ "$allow_flag" = "true" ]; then
    if [ -z "$allow_until" ] || [ "$allow_until" = "null" ] || [ "$allow_until" = "0" ]; then
      fail "ALLOW_ADMIN_CREATION=true without ALLOW_ADMIN_CREATION_UNTIL in production (P1-A regression)"
    fi
    if [ "$allow_until" -le "$now_epoch" ] 2>/dev/null; then
      ok "ALLOW_ADMIN_CREATION_UNTIL has already passed (gate auto-closed)"
    else
      log "  WARN: ALLOW_ADMIN_CREATION is open (until epoch=$allow_until, $((allow_until - now_epoch))s remaining)"
      ok "admin-bootstrap gate open but time-bounded (P1-A)"
    fi
  else
    ok "ALLOW_ADMIN_CREATION is false on the live task definition (P1-A)"
  fi
fi

ok "docs-e2e complete (full path)"
