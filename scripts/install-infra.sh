#!/usr/bin/env bash
#
# One-line infrastructure installer for a Stratoclave deployment (admin use).
#
#   curl -fsSL https://raw.githubusercontent.com/littlemex/stratoclave/main/scripts/install-infra.sh \
#     | STRATOCLAVE_ADMIN_EMAIL=admin@example.com bash
#
# Deploys the full stack to the AWS account of your CURRENT credentials. The
# account id is derived automatically (aws sts get-caller-identity) — you never
# type it. Only the admin email is required; region/prefix have sane defaults.
#
# Environment variables:
#   STRATOCLAVE_ADMIN_EMAIL   (required) email for the first admin user.
#   STRATOCLAVE_REGION        deploy region (default: us-east-1).
#   STRATOCLAVE_PREFIX        resource name prefix (default: stratoclave).
#   STRATOCLAVE_REF           git ref to deploy (default: main).
#   STRATOCLAVE_LOCKDOWN      "false" to leave admin-creation open (default: true
#                             -> redeploy with ALLOW_ADMIN_CREATION=false at the end).
#
# Prerequisites on the admin's machine: awscli v2 (authenticated), Node.js/npm,
# Docker (for the backend image build), and git.
set -euo pipefail

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YEL=$'\033[1;33m'; NC=$'\033[0m'
info() { printf '%s[stratoclave]%s %s\n' "$GREEN" "$NC" "$1"; }
warn() { printf '%s[stratoclave]%s %s\n' "$YEL" "$NC" "$1"; }
die()  { printf '%s[stratoclave]%s %s\n' "$RED" "$NC" "$1" >&2; exit 1; }

ADMIN_EMAIL="${STRATOCLAVE_ADMIN_EMAIL:-}"
[ -n "$ADMIN_EMAIL" ] || die "Set STRATOCLAVE_ADMIN_EMAIL (e.g. admin@example.com)."
export AWS_REGION="${STRATOCLAVE_REGION:-us-east-1}"
export STRATOCLAVE_PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"
REF="${STRATOCLAVE_REF:-main}"
LOCKDOWN="${STRATOCLAVE_LOCKDOWN:-true}"
REPO="https://github.com/littlemex/stratoclave.git"

for tool in aws node npm git docker; do
  command -v "$tool" >/dev/null || die "$tool is required but not found on PATH."
done

# Derive the account id from the caller's credentials — the admin never types it.
ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || die "aws sts get-caller-identity failed — are your AWS credentials configured?"
export CDK_DEFAULT_ACCOUNT="$ACCOUNT"
export CDK_DEFAULT_REGION="$AWS_REGION"
info "Deploying to account $ACCOUNT, region $AWS_REGION, prefix '$STRATOCLAVE_PREFIX'."
warn "This creates billable AWS resources. Ctrl-C within 5s to abort."
sleep 5

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
info "Cloning $REPO ($REF)…"
git clone --depth 1 --branch "$REF" "$REPO" "$WORK/stratoclave" >/dev/null 2>&1 \
  || git clone --depth 1 "$REPO" "$WORK/stratoclave" >/dev/null 2>&1 \
  || die "clone failed."
cd "$WORK/stratoclave"

info "Installing CDK deps (npm install)…"
( cd iac && npm install >/dev/null ) || die "npm install failed."

# Open the admin-bootstrap gate for the initial deploy, then close it at the end.
export ALLOW_ADMIN_CREATION=true

info "Deploying stacks (cdk) — 15-20 min…"
./iac/scripts/deploy-all.sh

info "Building & pushing the backend image to ECR…"
( cd iac && ./scripts/build-and-push.sh )

info "Forcing a new ECS deployment to pick up the image…"
aws ecs update-service --force-new-deployment \
  --cluster "${STRATOCLAVE_PREFIX}-cluster" --service "${STRATOCLAVE_PREFIX}-backend" \
  --region "$AWS_REGION" >/dev/null 2>&1 || warn "ecs update-service skipped (adjust names if customized)."
aws ecs wait services-stable --cluster "${STRATOCLAVE_PREFIX}-cluster" \
  --services "${STRATOCLAVE_PREFIX}-backend" --region "$AWS_REGION" 2>/dev/null || true

info "Bootstrapping the first admin ($ADMIN_EMAIL)…"
./scripts/bootstrap-admin.sh --email "$ADMIN_EMAIL"

if [ "$LOCKDOWN" = "true" ]; then
  info "Locking down the admin-creation gate (redeploy with ALLOW_ADMIN_CREATION=false)…"
  export ALLOW_ADMIN_CREATION=false
  ./iac/scripts/deploy-all.sh
fi

CF="$(aws cloudformation describe-stacks --region "$AWS_REGION" \
  --stack-name "${STRATOCLAVE_PREFIX}-frontend" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" --output text 2>/dev/null || true)"
info "Done."
[ -n "$CF" ] && printf '  Frontend URL: https://%s\n' "$CF"
printf '  Next: install the CLI on user machines with install-cli.sh and STRATOCLAVE_URL=https://%s\n' "${CF:-<cloudfront-domain>}"
