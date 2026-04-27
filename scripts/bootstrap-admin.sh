#!/usr/bin/env bash
#
# bootstrap-admin.sh — create the first Stratoclave admin user.
#
# Must be run after `iac/scripts/deploy-all.sh` has finished successfully
# and the Backend ECS service is healthy. Performs three idempotent steps:
#
#   1. Create (or reuse) a Cognito user with email_verified=true.
#   2. Set a permanent password (generated or user-supplied).
#   3. POST /api/mvp/admin/users to the Backend so the user has the
#      `admin` role in DynamoDB. Requires the Backend to be running with
#      ALLOW_ADMIN_CREATION=true.
#
# The `default-org` tenant and `admin` / `team_lead` / `user` permission
# rows are seeded automatically by the Backend at startup, so no manual
# DynamoDB steps are needed here.
#
# Usage:
#   ./scripts/bootstrap-admin.sh --email admin@example.com
#   ./scripts/bootstrap-admin.sh --email admin@example.com --password 'MyStrong!Pass123'
#   ./scripts/bootstrap-admin.sh --email admin@example.com --dry-run
#
# Environment:
#   AWS_PROFILE          AWS profile (optional)
#   AWS_REGION           AWS region (default: us-east-1)
#   STRATOCLAVE_PREFIX   Resource prefix (default: stratoclave)
#   API_ENDPOINT         Backend URL (default: from CloudFront stack output)

set -euo pipefail

# P1-6: keep any temp files we create in a 0700 directory and wipe them on
# exit regardless of success / failure. Previous versions wrote to a
# fixed /tmp/bootstrap-admin-resp.json that another user on the host
# could read.
TMPDIR_P1_6="$(mktemp -d -t stratoclave-bootstrap.XXXXXX)"
chmod 700 "$TMPDIR_P1_6"
cleanup_tmpdir() {
  rm -rf "$TMPDIR_P1_6"
}
trap cleanup_tmpdir EXIT INT TERM

EMAIL=""
PASSWORD=""
DRY_RUN=false
AWS_REGION_DEFAULT="${AWS_REGION:-us-east-1}"
PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --email)    EMAIL="$2"; shift 2 ;;
    --password) PASSWORD="$2"; shift 2 ;;
    --dry-run)  DRY_RUN=true; shift ;;
    -h|--help)
      sed -n '3,28p' "$0"
      exit 0
      ;;
    *) echo "[ERROR] Unknown argument: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$EMAIL" ]]; then
  echo "[ERROR] --email is required" >&2
  echo "  example: $0 --email admin@example.com" >&2
  exit 1
fi

# Email validation (very loose — full RFC 5322 is not worth it for a CLI hint).
if [[ ! "$EMAIL" =~ ^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$ ]]; then
  echo "[ERROR] Email does not look valid: $EMAIL" >&2
  exit 1
fi

COGNITO_STACK="${PREFIX}-cognito"
FRONTEND_STACK="${PREFIX}-frontend"

get_stack_output() {
  local stack_name=$1
  local output_key=$2
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$AWS_REGION_DEFAULT" \
    --query "Stacks[0].Outputs[?OutputKey==\`$output_key\`].OutputValue" \
    --output text 2>/dev/null
}

echo "[INFO] Resolving deployment outputs in region $AWS_REGION_DEFAULT..."
USER_POOL_ID=$(get_stack_output "$COGNITO_STACK" "UserPoolId")
if [[ -z "$USER_POOL_ID" || "$USER_POOL_ID" == "None" ]]; then
  echo "[ERROR] Could not resolve UserPoolId from $COGNITO_STACK." >&2
  echo "  Is the Cognito stack deployed? Run iac/scripts/deploy-all.sh first." >&2
  exit 1
fi

CLOUDFRONT_DOMAIN=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")
API_ENDPOINT="${API_ENDPOINT:-}"
if [[ -z "$API_ENDPOINT" && -n "$CLOUDFRONT_DOMAIN" ]]; then
  API_ENDPOINT="https://${CLOUDFRONT_DOMAIN}"
fi
if [[ -z "$API_ENDPOINT" ]]; then
  echo "[ERROR] Could not resolve API_ENDPOINT. Pass --api-endpoint or deploy $FRONTEND_STACK first." >&2
  exit 1
fi

echo "[INFO] User Pool : $USER_POOL_ID"
echo "[INFO] API        : $API_ENDPOINT"
echo "[INFO] Email      : $EMAIL"

if [[ -z "$PASSWORD" ]]; then
  PASSWORD=$(openssl rand -base64 24 | tr -d '=+/' | cut -c1-20)
  PASSWORD="${PASSWORD}Aa1!"
  echo "[INFO] Generated a random password (shown once at the end)."
fi

if [[ "$DRY_RUN" == true ]]; then
  echo ""
  echo "[DRY RUN] Would perform:"
  echo "  1. aws cognito-idp admin-create-user --user-pool-id $USER_POOL_ID --username $EMAIL ..."
  echo "  2. aws cognito-idp admin-set-user-password --password <permanent> ..."
  echo "  3. POST $API_ENDPOINT/api/mvp/admin/users { email: $EMAIL, roles: ['admin'], tenant_id: 'default-org' }"
  exit 0
fi

# --- 1. Create Cognito user (idempotent) ---
echo ""
echo "[STEP 1/3] Ensuring Cognito user exists"
if aws cognito-idp admin-get-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "$EMAIL" \
    --region "$AWS_REGION_DEFAULT" \
    >/dev/null 2>&1; then
  echo "[INFO] User already exists. Skipping create."
else
  aws cognito-idp admin-create-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "$EMAIL" \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
    --temporary-password "$(openssl rand -base64 12 | tr -d '=+/' | cut -c1-10)Aa1!" \
    --message-action SUPPRESS \
    --region "$AWS_REGION_DEFAULT" \
    >/dev/null
  echo "[OK]   Created Cognito user."
fi

# --- 2. Set permanent password ---
echo ""
echo "[STEP 2/3] Setting permanent password"
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --password "$PASSWORD" \
  --permanent \
  --region "$AWS_REGION_DEFAULT" \
  >/dev/null
echo "[OK]   Password set (permanent)."

# --- 3. Promote to admin via Backend API ---
echo ""
echo "[STEP 3/3] Granting admin role via Backend"
echo "[INFO] Requires the Backend to be running with ALLOW_ADMIN_CREATION=true"

BOOTSTRAP_URL="${API_ENDPOINT%/}/api/mvp/admin/users"
BODY=$(cat <<JSON
{
  "email": "$EMAIL",
  "roles": ["admin"],
  "tenant_id": "default-org",
  "bootstrap": true
}
JSON
)

if ! command -v curl >/dev/null; then
  echo "[ERROR] curl is required for step 3." >&2
  exit 1
fi

RESP_FILE="$TMPDIR_P1_6/resp.json"
set +e
HTTP_STATUS=$(curl -sS -o "$RESP_FILE" -w '%{http_code}' \
  -X POST "$BOOTSTRAP_URL" \
  -H 'Content-Type: application/json' \
  -H 'X-Bootstrap: true' \
  -d "$BODY")
CURL_EXIT=$?
set -e

if [[ "$CURL_EXIT" -ne 0 ]]; then
  echo "[ERROR] curl failed (exit $CURL_EXIT). Is the Backend reachable at $API_ENDPOINT?" >&2
  exit 1
fi

case "$HTTP_STATUS" in
  200|201)
    echo "[OK]   Admin role granted."
    ;;
  409)
    echo "[INFO] User already has a role assigned (409). Treating as success."
    ;;
  401|403)
    echo "[ERROR] Backend rejected the request ($HTTP_STATUS)." >&2
    echo "  The Backend must be deployed with ALLOW_ADMIN_CREATION=true for the" >&2
    echo "  initial bootstrap. After the first admin exists, set ALLOW_ADMIN_CREATION=false." >&2
    cat "$RESP_FILE" >&2 || true
    exit 1
    ;;
  *)
    echo "[ERROR] Unexpected Backend response: HTTP $HTTP_STATUS" >&2
    cat "$RESP_FILE" >&2 || true
    exit 1
    ;;
esac

# --- Summary ---
# P1-6: By default print the password to stderr so it is not captured in
# the typical "./bootstrap-admin.sh > log.txt" redirection, and offer an
# alternative to write it (0600) to a file the operator chooses. Email
# and URL stay on stdout because they are safe to capture.
echo ""
echo "============================================"
echo " Bootstrap complete"
echo "============================================"
echo "  Email:      $EMAIL"
echo "  Login URL:  $API_ENDPOINT"
echo ""

if [[ -n "${STRATOCLAVE_PASSWORD_FILE:-}" ]]; then
  # Write password to the specified file with 0600 permissions.
  umask 077
  printf '%s\n' "$PASSWORD" > "$STRATOCLAVE_PASSWORD_FILE"
  echo "  Password:   written to $STRATOCLAVE_PASSWORD_FILE (0600)"
else
  # Emit to stderr so `./bootstrap-admin.sh > log.txt` does not capture
  # the password into a shared log file.
  {
    echo "  Password:   $PASSWORD"
    echo "              (printed on stderr; capture with 2>> or set"
    echo "               STRATOCLAVE_PASSWORD_FILE=<path> to write 0600)"
  } >&2
fi

echo ""
echo "Share the login URL with the administrator. Once they have logged in"
echo "at least once, consider redeploying the Backend with:"
echo "  ALLOW_ADMIN_CREATION=false"
echo "to lock down the bootstrap endpoint."
