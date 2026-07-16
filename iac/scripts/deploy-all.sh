#!/bin/bash
set -euo pipefail

###############################################################################
# Stratoclave Full Deployment Script
#
# Deploys all CDK stacks in dependency order, then builds and deploys the
# Frontend assets to S3 / CloudFront. The Cognito callback URL is resolved
# via a cross-stack reference (`frontendStack.cfnDistribution.attrDomainName`),
# so no second Cognito deploy is necessary.
#
# Stack order (see iac/bin/iac.ts):
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack
#   5. <Prefix>FrontendStack      (S3 + CloudFront)
#   6. <Prefix>CognitoStack       (receives CloudFront domain via cross-stack)
#   7. <Prefix>EcsStack
#   8. <Prefix>ConfigStack        (SSM parameters for runtime consumers)
#
# Then:
#   9. Build the Frontend with the CDK outputs embedded and sync to S3
#  10. Invalidate CloudFront
#
# Admin user creation is **NOT** performed here. Run `scripts/bootstrap-admin.sh`
# after the stack is healthy to create the first administrator.
#
# Usage:
#   ./deploy-all.sh              # Deploy everything
#   ./deploy-all.sh --skip-build # Skip the Frontend build step
#   ./deploy-all.sh --dry-run    # Print the plan without executing
#
# Environment variables:
#   AWS_PROFILE           AWS profile (optional)
#   STRATOCLAVE_REGION    Body-stack deploy region R (default: us-east-1). The
#                         WAF stack is always us-east-1 (CLOUDFRONT scope); when
#                         R != us-east-1 you must `cdk bootstrap` BOTH regions.
#   CDK_DEFAULT_REGION    Fallback for the body region if STRATOCLAVE_REGION unset
#   BEDROCK_PRIMARY_REGION Bedrock model primary region (required when R != us-east-1)
#   STRATOCLAVE_PREFIX    Resource prefix (default: stratoclave)
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$IAC_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
# Body region R: the frontend S3 bucket and all queried stacks (network,
# dynamodb, ecr, alb, frontend, cognito, ecs, config) live here. Only the WAF
# stack lives in us-east-1; this script never queries it, so REGION == R.
REGION="${STRATOCLAVE_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}"
PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"
SKIP_BUILD=false
DRY_RUN=false
DEPLOYMENT_LOG="$IAC_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-build) SKIP_BUILD=true; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --help|-h)
      sed -n '3,35p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# --- Logging helpers ---
log_info()  { echo "[INFO]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_step()  { echo ""; echo "[STEP]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; echo "---"; }
log_error() { echo "[ERROR] $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG" >&2; }
log_warn()  { echo "[WARN]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_ok()    { echo "[OK]    $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }

on_error() {
  local exit_code=$?
  local line_no=$1
  log_error "Deployment failed at line $line_no with exit code $exit_code"
  echo ""
  echo "Troubleshooting:"
  echo "  1. Check the log: $DEPLOYMENT_LOG"
  echo "  2. Inspect CloudFormation events:"
  echo "     aws cloudformation describe-stack-events --stack-name <STACK_NAME> --region $REGION"
  echo "  3. Retry — this script is idempotent."
  exit $exit_code
}
trap 'on_error $LINENO' ERR

# --- Stack names (kebab-case, matches iac/lib/_common.ts stackName()) ---
NETWORK_STACK="${PREFIX}-network"
DYNAMODB_STACK="${PREFIX}-dynamodb"
ECR_STACK="${PREFIX}-ecr"
ALB_STACK="${PREFIX}-alb"
FRONTEND_STACK="${PREFIX}-frontend"
COGNITO_STACK="${PREFIX}-cognito"
ECS_STACK="${PREFIX}-ecs"
CONFIG_STACK="${PREFIX}-config"

get_stack_output() {
  local stack_name=$1
  local output_key=$2
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`$output_key\`].OutputValue" \
    --output text 2>/dev/null
}

stack_exists() {
  local stack_name=$1
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$REGION" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null | grep -qvE "DELETE_COMPLETE|ROLLBACK_COMPLETE"
}

deploy_stacks() {
  if [ "$DRY_RUN" = true ]; then
    log_info "[DRY RUN] Would deploy: $*"
    return 0
  fi
  cd "$IAC_DIR"
  npx cdk deploy "$@" --require-approval never 2>&1 | tee -a "$DEPLOYMENT_LOG"
}

# --- Pre-flight ---
log_step "Pre-flight checks"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  log_error "AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  exit 1
}
log_info "AWS account: $ACCOUNT"
log_info "AWS region:  $REGION"
log_info "Prefix:      $PREFIX"

command -v node >/dev/null || { log_error "Node.js 20+ is required."; exit 1; }
log_info "Node: $(node --version)"

# Env preflight (Fable consult D3). When the body region != us-east-1, both the
# deploy AND `cdk bootstrap` (which synthesizes bin/iac.ts) require an explicit
# BEDROCK_PRIMARY_REGION — otherwise the app throws with a message that is easy
# to misread as a code bug. Surface the requirement here, before anything runs.
# Use ${VAR:-} so an UNSET var does not abort under `set -u` — this preflight
# exists precisely for the unset case, so referencing it unguarded would crash
# before the helpful message ever prints. (Fable final review B-2)
if [ "$REGION" != "us-east-1" ] && [ -z "${BEDROCK_PRIMARY_REGION:-}" ]; then
  log_error "BEDROCK_PRIMARY_REGION must be set when STRATOCLAVE_REGION ($REGION) != us-east-1."
  log_error "  It is the Bedrock MODEL region (independent of the deploy region) and is"
  log_error "  required both for this deploy and for 'cdk bootstrap' (bootstrap synths the app)."
  log_error "  Example: export BEDROCK_PRIMARY_REGION=us-east-1   # or =$REGION for in-region models"
  exit 1
fi

# Cross-region bootstrap check (Fable review M-1). When WAF is on and the body
# region != us-east-1, the WAF stack lives in us-east-1 and its cross-region
# export writer needs BOTH regions bootstrapped; a missing CDKToolkit surfaces
# as an opaque custom-resource error mid-deploy. Fail fast with a clear message.
# Skipped under --dry-run (no AWS mutation, and dry-run should not require creds
# beyond the identity check already done). (Fable final review B-2)
WAF_REGION="us-east-1"
ENABLE_WAF_EFFECTIVE="$(echo "${ENABLE_WAF:-true}" | tr '[:upper:]' '[:lower:]')"
check_bootstrap() {
  local r=$1
  aws cloudformation describe-stacks --stack-name CDKToolkit --region "$r" &>/dev/null || {
    log_error "Region $r is not cdk-bootstrapped. Run: npx cdk bootstrap aws://$ACCOUNT/$r"
    exit 1
  }
}
if [ "$DRY_RUN" != true ]; then
  check_bootstrap "$REGION"
  if [ "$ENABLE_WAF_EFFECTIVE" != "false" ] && [ "$REGION" != "$WAF_REGION" ]; then
    log_info "Body region $REGION != $WAF_REGION and WAF is on: checking us-east-1 bootstrap (cross-region WAF export)."
    check_bootstrap "$WAF_REGION"
  fi
fi

if [ ! -d "$IAC_DIR/node_modules" ]; then
  log_info "Installing CDK dependencies..."
  cd "$IAC_DIR" && npm install
fi

log_info "Type-checking CDK..."
cd "$IAC_DIR" && npx tsc --noEmit 2>&1 | tee -a "$DEPLOYMENT_LOG" || {
  log_error "TypeScript compilation failed. Fix errors in iac/lib/ before retrying."
  exit 1
}

if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "Plan:"
  echo "  1. $NETWORK_STACK"
  echo "  2. $DYNAMODB_STACK"
  echo "  3. $ECR_STACK"
  echo "  4. $ALB_STACK"
  echo "  5. $FRONTEND_STACK"
  echo "  6. $COGNITO_STACK"
  echo "  7. $ECS_STACK"
  echo "  8. $CONFIG_STACK"
  if [ "$SKIP_BUILD" = false ]; then
    echo "  9. Build Frontend, upload to S3, invalidate CloudFront"
  fi
  exit 0
fi

# --- Deploy all stacks in one shot (CDK resolves dependencies automatically) ---
log_step "Deploying all CDK stacks"
deploy_stacks --all
log_ok "All stacks deployed"

# --- Build and deploy frontend ---
if [ "$SKIP_BUILD" = true ]; then
  log_warn "Skipping Frontend build (--skip-build)"
else
  log_step "Building and deploying Frontend assets"

  COGNITO_CLIENT_ID=$(get_stack_output "$COGNITO_STACK" "UserPoolClientId")
  COGNITO_USER_POOL_ID=$(get_stack_output "$COGNITO_STACK" "UserPoolId")
  COGNITO_DOMAIN=$(get_stack_output "$COGNITO_STACK" "CognitoDomain")
  CLOUDFRONT_DOMAIN=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")
  FRONTEND_BUCKET=$(get_stack_output "$FRONTEND_STACK" "FrontendBucketName")
  DISTRIBUTION_ID=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDistributionId")

  for var in COGNITO_CLIENT_ID COGNITO_USER_POOL_ID COGNITO_DOMAIN CLOUDFRONT_DOMAIN FRONTEND_BUCKET DISTRIBUTION_ID; do
    if [ -z "${!var}" ]; then
      log_error "Stack output $var is empty. Check that all stacks deployed successfully."
      exit 1
    fi
  done

  log_info "CloudFront domain: $CLOUDFRONT_DOMAIN"
  log_info "Cognito client ID: $COGNITO_CLIENT_ID"
  log_info "Frontend bucket:   $FRONTEND_BUCKET"

  cd "$FRONTEND_DIR"
  [ -d node_modules ] || npm ci 2>&1 | tee -a "$DEPLOYMENT_LOG"
  npm run build 2>&1 | tee -a "$DEPLOYMENT_LOG"

  if [ ! -d dist ]; then
    log_error "Frontend build failed: dist/ not produced"
    exit 1
  fi

  log_info "Generating dist/config.json from stack outputs"
  cat > dist/config.json <<CONFIGEOF
{
  "cognito": {
    "user_pool_id": "$COGNITO_USER_POOL_ID",
    "client_id": "$COGNITO_CLIENT_ID",
    "domain": "https://$COGNITO_DOMAIN",
    "region": "$REGION"
  },
  "api": {
    "endpoint": ""
  },
  "app": {
    "cloudfront_domain": "$CLOUDFRONT_DOMAIN"
  }
}
CONFIGEOF

  aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" --delete --region "$REGION" \
    2>&1 | tee -a "$DEPLOYMENT_LOG"

  aws cloudfront create-invalidation \
    --distribution-id "$DISTRIBUTION_ID" \
    --paths "/*" \
    --region "$REGION" \
    --output json 2>&1 | tee -a "$DEPLOYMENT_LOG"

  log_ok "Frontend built and deployed"
fi

# --- Summary ---
CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")}
ALB_DNS=$(get_stack_output "$ALB_STACK" "AlbDnsName" 2>/dev/null || echo "N/A")
COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID:-$(get_stack_output "$COGNITO_STACK" "UserPoolId" 2>/dev/null || echo "N/A")}

echo ""
echo "============================================"
echo "[SUCCESS] Deployment completed"
echo "============================================"
echo ""
echo "  Frontend URL:  https://$CLOUDFRONT_DOMAIN"
echo "  ALB endpoint:  http://$ALB_DNS"
echo "  User Pool ID:  $COGNITO_USER_POOL_ID"
echo ""
echo "Next steps:"
echo "  1. Push the Backend container image:"
echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
echo "  2. Create the first admin user:"
echo "     $PROJECT_ROOT/scripts/bootstrap-admin.sh --email admin@example.com"
echo "  3. Share the CloudFront URL with CLI users:"
echo "     stratoclave setup https://$CLOUDFRONT_DOMAIN"
echo ""
echo "Log: $DEPLOYMENT_LOG"
