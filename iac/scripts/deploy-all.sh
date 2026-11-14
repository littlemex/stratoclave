#!/bin/bash
set -euo pipefail

###############################################################################
# Stratoclave Full Deployment Script
#
# Deploys all CDK stacks in dependency order, then — in the default
# `cloudfront` entry mode only — builds and deploys the Frontend assets to
# S3 / CloudFront. The Cognito callback URL is resolved via a cross-stack
# reference (`frontendStack.cfnDistribution.attrDomainName`), so no second
# Cognito deploy is necessary.
#
# Stack order, `STRATOCLAVE_ENTRY=cloudfront` (default; see iac/bin/iac.ts):
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack           (internet-facing)
#   5. <Prefix>WafStack           (CLOUDFRONT scope, us-east-1)
#   6. <Prefix>FrontendStack      (S3 + CloudFront)
#   7. <Prefix>CognitoStack       (receives CloudFront domain via cross-stack)
#   8. <Prefix>EcsStack
#   9. <Prefix>ConfigStack        (SSM parameters for runtime consumers)
#
# Then:
#  10. Build the Frontend with the CDK outputs embedded and sync to S3
#  11. Invalidate CloudFront
#
# Stack order, `STRATOCLAVE_ENTRY=private` (headless, PrivateLink only):
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack           (internal; no port 80)
#   5. <Prefix>PrivateLinkStack   (internal NLB + VPC endpoint service)
#   6. <Prefix>CognitoStack
#   7. <Prefix>EcsStack
#   8. <Prefix>ConfigStack
# No WafStack, no FrontendStack: steps 10/11 above (frontend build, S3
# upload, CloudFront invalidation) are skipped entirely — there is no S3
# bucket or CloudFront distribution to put anything into. See
# docs/DEPLOYMENT.md, "Private entry (PrivateLink), headless".
#
# ECR push ordering (same in BOTH entry modes, not affected by this
# variable): this script deploys infrastructure only. The ECS service in
# either mode starts with whatever `IMAGE_TAG` tag is already in the ECR
# repository `<Prefix>EcrStack` just created — on a fresh deployment that is
# nothing, so the backend task fails its health checks until
# `scripts/build-and-push.sh` has pushed an image. Run that script AFTER
# this one, in both entry modes, exactly as "Post-deploy: container image"
# in docs/DEPLOYMENT.md describes; nothing about entry mode changes that
# ordering.
#
# NOT deployed here: the quota gated stacks (<Prefix>QuotaReconcilerStack,
# <Prefix>QuotaGrantsStack). They need the Lambda image built from
# backend/Dockerfile.lambda, which does not exist until `build-and-push.sh` has
# run once, so they are a REQUIRED second pass documented in the "Next steps"
# this script prints and in docs/DEPLOYMENT.md ("Post-deploy: quota gated
# stacks") — not an optional extra.
#
# Admin user creation is **NOT** performed here. See docs/DEPLOYMENT.md,
# "Post-deploy: first admin" (works identically in both entry modes; it is a
# Cognito API call the backend itself makes, not something a console does).
#
# Usage:
#   ./deploy-all.sh              # Deploy everything
#   ./deploy-all.sh --skip-build # Skip the Frontend build step (cloudfront mode only)
#   ./deploy-all.sh --dry-run    # Print the plan without executing
#
# Environment variables:
#   AWS_PROFILE           AWS profile (optional)
#   STRATOCLAVE_ENTRY     'cloudfront' (default) or 'private'. Switching an
#                         EXISTING deployment between the two is refused by
#                         the pre-flight check below — it would replace the
#                         live ALB in place and take the deployment down.
#                         Deploy the other mode under a fresh STRATOCLAVE_PREFIX.
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
ENTRY="${STRATOCLAVE_ENTRY:-cloudfront}"
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
PRIVATE_LINK_STACK="${PREFIX}-private-link"
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

command -v node >/dev/null || { log_error "Node.js 22 is required (see .nvmrc; exact ranges in the engines field of frontend/package.json and iac/package.json)."; exit 1; }
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

# Entry-mode switch guard. Switching STRATOCLAVE_ENTRY on an EXISTING
# deployment is not supported: AlbStack's internetFacing/internal scheme
# cannot change in place (CloudFormation replaces the load balancer), so a
# `cloudfront`-mode prefix redeployed as `private` would tear down the live
# internet-facing ALB and stand up an internal one under the same logical
# id — taking the current deployment down mid-flight, not failing cleanly.
# Checked both directions, read-only, before anything is touched. Skipped
# under --dry-run (consistent with the bootstrap checks above: dry-run
# should not require credentials beyond the identity check already done).
if [ "$DRY_RUN" != true ]; then
  if [ "$ENTRY" = "private" ] && stack_exists "$FRONTEND_STACK"; then
    log_error "STRATOCLAVE_ENTRY=private, but $FRONTEND_STACK already exists in $REGION."
    log_error "  Switching an existing deployment from cloudfront to private is not"
    log_error "  supported: AlbStack would change from internet-facing to internal in"
    log_error "  place, which CloudFormation does by replacing the load balancer —"
    log_error "  taking the live deployment down, not refusing cleanly."
    log_error "  Deploy private under a fresh STRATOCLAVE_PREFIX instead."
    exit 1
  fi
  if [ "$ENTRY" != "private" ] && stack_exists "$PRIVATE_LINK_STACK"; then
    log_error "STRATOCLAVE_ENTRY=$ENTRY, but $PRIVATE_LINK_STACK already exists in $REGION."
    log_error "  Switching an existing deployment from private to cloudfront is not"
    log_error "  supported, for the same reason in reverse."
    log_error "  Deploy cloudfront under a fresh STRATOCLAVE_PREFIX instead."
    exit 1
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
  echo "Plan (STRATOCLAVE_ENTRY=$ENTRY):"
  echo "  1. $NETWORK_STACK"
  echo "  2. $DYNAMODB_STACK"
  echo "  3. $ECR_STACK"
  echo "  4. $ALB_STACK"
  if [ "$ENTRY" = "private" ]; then
    echo "  5. $PRIVATE_LINK_STACK"
    echo "  6. $COGNITO_STACK"
    echo "  7. $ECS_STACK"
    echo "  8. $CONFIG_STACK"
    echo "  (no WAF stack, no Frontend stack: headless, no CloudFront, no S3 console)"
  else
    echo "  5. ${PREFIX}-waf"
    echo "  6. $FRONTEND_STACK"
    echo "  7. $COGNITO_STACK"
    echo "  8. $ECS_STACK"
    echo "  9. $CONFIG_STACK"
    if [ "$SKIP_BUILD" = false ]; then
      echo "  10. Build Frontend, upload to S3, invalidate CloudFront"
    fi
  fi
  exit 0
fi

# --- Deploy all stacks in one shot (CDK resolves dependencies automatically) ---
log_step "Deploying all CDK stacks"
deploy_stacks --all
log_ok "All stacks deployed"

# --- Build and deploy frontend (cloudfront mode only: private mode has no
# CloudFront distribution or S3 console bucket to build or upload anything
# to) ---
if [ "$ENTRY" = "private" ]; then
  log_info "STRATOCLAVE_ENTRY=private: no frontend build/upload/invalidation (headless; no web console on this path)."
elif [ "$SKIP_BUILD" = true ]; then
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
    "domain": "$COGNITO_DOMAIN",
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
ALB_DNS=$(get_stack_output "$ALB_STACK" "AlbDnsName" 2>/dev/null || echo "N/A")
COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID:-$(get_stack_output "$COGNITO_STACK" "UserPoolId" 2>/dev/null || echo "N/A")}

echo ""
echo "============================================"
echo "[SUCCESS] Deployment completed"
echo "============================================"
echo ""

if [ "$ENTRY" = "private" ]; then
  PRIVATE_URL=$(get_stack_output "$PRIVATE_LINK_STACK" "PrivateUrl")
  ENDPOINT_SERVICE_NAME=$(get_stack_output "$PRIVATE_LINK_STACK" "EndpointServiceName")
  echo "  Private URL:       $PRIVATE_URL"
  echo "  Endpoint service:  $ENDPOINT_SERVICE_NAME"
  echo "  ALB (internal):    $ALB_DNS"
  echo "  User Pool ID:      $COGNITO_USER_POOL_ID"
  echo ""
  echo "Next steps (STRATOCLAVE_ENTRY=private):"
  echo "  1. Push the Backend container image (same ordering as cloudfront mode:"
  echo "     this runs after the stacks above regardless of entry mode; also"
  echo "     builds+pushes the scheduled-Lambda image and prints the"
  echo "     LAMBDA_IMAGE_TAG the quota-stack pass below needs):"
  echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
  echo "  2. REQUIRED second pass — deploy the quota gated stacks (skipping this leaves"
  echo "     limit-raise grants that never expire and tenant pools that vanish on the"
  echo "     1st for any tenant whose membership did not change that month):"
  echo "     cd $IAC_DIR && LAMBDA_IMAGE_TAG=<tag from step 1> npx cdk deploy \\"
  echo "       ${PREFIX}-quota-reconciler ${PREFIX}-quota-grants \\"
  echo "       -c quotaReconciler=true -c quotaGrants=true --require-approval never"
  echo "  3. Accept the pending VPC endpoint connection from each approved consumer"
  echo "     account, and have each consumer create their interface endpoint and"
  echo "     private DNS. See docs/DEPLOYMENT.md, 'Private entry (PrivateLink),"
  echo "     headless' for both sides of that exchange."
  echo "  4. Create the first admin/API key without a console — see"
  echo "     docs/DEPLOYMENT.md, 'Private entry (PrivateLink), headless', the"
  echo "     'Bootstrapping without a console' subsection."
else
  CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")}
  echo "  Frontend URL:  https://$CLOUDFRONT_DOMAIN"
  echo "  ALB endpoint:  http://$ALB_DNS"
  echo "  User Pool ID:  $COGNITO_USER_POOL_ID"
  echo ""
  echo "Next steps:"
  echo "  1. Push the Backend container image (also builds+pushes the scheduled-Lambda"
  echo "     image and prints the LAMBDA_IMAGE_TAG the next step needs):"
  echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
  echo "  2. REQUIRED second pass — deploy the quota gated stacks (skipping this leaves"
  echo "     limit-raise grants that never expire and tenant pools that vanish on the"
  echo "     1st for any tenant whose membership did not change that month):"
  echo "     cd $IAC_DIR && LAMBDA_IMAGE_TAG=<tag from step 1> npx cdk deploy \\"
  echo "       ${PREFIX}-quota-reconciler ${PREFIX}-quota-grants \\"
  echo "       -c quotaReconciler=true -c quotaGrants=true --require-approval never"
  echo "  3. Create the first admin user (bootstrap-admin.sh is stale — see"
  echo "     docs/DEPLOYMENT.md 'Post-deploy: first admin'):"
  echo "     export STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL=admin@example.com"
  echo "     cd $IAC_DIR && npx cdk deploy ${PREFIX}-ecs --require-approval never"
  echo "     aws secretsmanager get-secret-value --secret-id ${PREFIX}/bootstrap-admin-temp-password --query SecretString --output text"
  echo "  4. Share the CloudFront URL with CLI users:"
  echo "     stratoclave setup https://$CLOUDFRONT_DOMAIN"
fi
echo ""
echo "Log: $DEPLOYMENT_LOG"
