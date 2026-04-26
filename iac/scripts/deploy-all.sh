#!/bin/bash
set -euo pipefail

###############################################################################
# Stratoclave Full Deployment Script
#
# This script deploys all CDK stacks in the correct dependency order and
# handles the circular dependency between Cognito (needs CloudFront callback
# URL) and Frontend (needs ALB which is independent of Cognito).
#
# Deployment order:
#   1. Infrastructure stacks (Network, ECR, ALB, RDS, Redis, WAF, CodeBuild)
#   2. Cognito (initial deploy without CloudFront callback URL)
#   3. Verified Permissions (depends on Cognito)
#   4. ECS (depends on Cognito, VP, RDS, Redis)
#   5. Frontend (S3 + CloudFront)
#   6. Cognito update (add CloudFront callback URL)
#   7. Frontend CodeBuild
#   8. Build and deploy frontend assets
#   9. Create admin user (initial deploy only)
#
# Usage:
#   ./deploy-all.sh              # Deploy all stacks
#   ./deploy-all.sh --skip-build # Deploy stacks but skip frontend build
#   ./deploy-all.sh --dry-run    # Show what would be deployed
#
# Environment Variables:
#   CDK_DEFAULT_REGION  - AWS region (default: us-east-1)
#   AWS_PROFILE         - AWS profile to use
#   ADMIN_EMAIL         - Admin user email (default: admin@stratoclave.com)
###############################################################################

# --- Configuration ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$IAC_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
REGION="${CDK_DEFAULT_REGION:-us-east-1}"
COGNITO_REGION="us-east-1"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@stratoclave.com}"
SKIP_BUILD=false
DRY_RUN=false
DEPLOYMENT_LOG="$IAC_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"

# --- Argument Parsing ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-build)
      SKIP_BUILD=true
      shift
      ;;
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --skip-build  Skip frontend build and S3 upload"
      echo "  --dry-run     Show deployment plan without executing"
      echo "  --help        Show this help message"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1"
      exit 1
      ;;
  esac
done

# --- Logging Helpers ---
log_info()  { echo "[INFO]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_step()  { echo ""; echo "[STEP]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; echo "---"; }
log_error() { echo "[ERROR] $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG" >&2; }
log_warn()  { echo "[WARN]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_ok()    { echo "[OK]    $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }

# --- Error Handler ---
on_error() {
  local exit_code=$?
  local line_no=$1
  log_error "Deployment failed at line $line_no with exit code $exit_code"
  echo ""
  echo "============================================"
  echo "[FAILED] Deployment failed"
  echo "============================================"
  echo ""
  echo "Troubleshooting steps:"
  echo "  1. Check the deployment log: $DEPLOYMENT_LOG"
  echo "  2. Check CloudFormation events:"
  echo "     aws cloudformation describe-stack-events --stack-name <STACK_NAME> --region $REGION"
  echo "  3. Retry from the failed step (the script is idempotent)"
  echo ""
  echo "To rollback a specific stack:"
  echo "  npx cdk destroy <STACK_NAME> --require-approval never"
  echo ""
  echo "Common issues:"
  echo "  - S3 bucket name conflicts: Check if bucket already exists in another account"
  echo "  - Cognito domain prefix conflicts: Change COGNITO_DOMAIN_PREFIX env var"
  echo "  - ECR image missing: Run ./scripts/cloud-build.sh after deployment"
  exit $exit_code
}
trap 'on_error $LINENO' ERR

# --- Helper Functions ---

# Retrieve a CloudFormation stack output value
get_stack_output() {
  local stack_name=$1
  local output_key=$2
  local region=${3:-$REGION}
  local value
  value=$(aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$region" \
    --query "Stacks[0].Outputs[?OutputKey==\`$output_key\`].OutputValue" \
    --output text 2>/dev/null || echo "")
  if [ -z "$value" ] || [ "$value" = "None" ]; then
    log_error "Failed to retrieve $output_key from $stack_name (region: $region)"
    return 1
  fi
  echo "$value"
}

# Check if a CloudFormation stack exists
stack_exists() {
  local stack_name=$1
  local region=${2:-$REGION}
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$region" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null | grep -qvE "DELETE_COMPLETE|ROLLBACK_COMPLETE"
}

# Check if a Cognito user exists
cognito_user_exists() {
  local pool_id=$1
  local username=$2
  aws cognito-idp admin-get-user \
    --user-pool-id "$pool_id" \
    --username "$username" \
    --region "$COGNITO_REGION" \
    --output text 2>/dev/null && return 0 || return 1
}

# Deploy one or more CDK stacks
deploy_stacks() {
  if [ "$DRY_RUN" = true ]; then
    log_info "[DRY RUN] Would deploy: $*"
    return 0
  fi
  cd "$IAC_DIR"
  npx cdk deploy "$@" --require-approval never 2>&1 | tee -a "$DEPLOYMENT_LOG"
}

# --- Pre-flight Checks ---
log_step "Pre-flight Checks"

# Check AWS credentials
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  log_error "AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  exit 1
}
log_info "AWS Account: $ACCOUNT"
log_info "AWS Region: $REGION"
log_info "Cognito Region: $COGNITO_REGION"

# Check Node.js
if ! command -v node &>/dev/null; then
  log_error "Node.js is required. Install Node.js 20+ from https://nodejs.org/"
  exit 1
fi
NODE_VERSION=$(node --version)
log_info "Node.js: $NODE_VERSION"

# Check CDK dependencies
if [ ! -d "$IAC_DIR/node_modules" ]; then
  log_info "Installing CDK dependencies..."
  cd "$IAC_DIR" && npm install
fi

# Compile TypeScript
log_info "Compiling CDK TypeScript..."
cd "$IAC_DIR" && npx tsc 2>&1 | tee -a "$DEPLOYMENT_LOG" || {
  log_error "TypeScript compilation failed. Fix errors in iac/lib/ and retry."
  exit 1
}

# Determine initial vs. update deployment
IS_INITIAL_DEPLOY=false
if ! stack_exists "StratoclaveNetworkStack"; then
  IS_INITIAL_DEPLOY=true
  log_info "Detected: INITIAL deployment (no existing stacks found)"
else
  log_info "Detected: UPDATE deployment (existing stacks found)"
fi

# Dry run summary
if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "============================================"
  echo "[DRY RUN] Deployment Plan"
  echo "============================================"
  echo ""
  echo "Stacks to deploy (in order):"
  echo "  1. StratoclaveNetworkStack"
  echo "  2. StratoclaveEcrStack"
  echo "  3. StratoclaveAlbStack"
  echo "  4. StratoclaveRdsStack"
  echo "  5. StratoclaveRedisStack"
  echo "  6. StratoclaveWafStack"
  echo "  7. StratoclaveCodeBuildStack"
  echo "  8. StratoclaveCognitoStack (without CloudFront callback)"
  echo "  9. StratoclaveVpStack"
  echo "  10. StratoclaveEcsStack"
  echo "  11. StratoclaveFrontendStack"
  echo "  12. StratoclaveCognitoStack (update with CloudFront callback)"
  echo "  13. StratoclaveFrontendCodeBuildStack"
  if [ "$SKIP_BUILD" = false ]; then
    echo "  14. Frontend build + S3 deploy + CloudFront invalidation"
  fi
  if [ "$IS_INITIAL_DEPLOY" = true ]; then
    echo "  15. Create admin user ($ADMIN_EMAIL)"
  fi
  echo ""
  exit 0
fi

# --- Step 1: Infrastructure Stacks ---
log_step "Step 1/9: Deploying infrastructure stacks (Network, ECR, ALB, RDS, Redis, WAF, CodeBuild)"

deploy_stacks \
  StratoclaveNetworkStack \
  StratoclaveEcrStack \
  StratoclaveAlbStack \
  StratoclaveRdsStack \
  StratoclaveRedisStack \
  StratoclaveWafStack \
  StratoclaveCodeBuildStack

log_ok "Infrastructure stacks deployed"

# --- Step 2: Cognito Stack (initial, without CloudFront callback) ---
log_step "Step 2/9: Deploying Cognito Stack (initial, without CloudFront callback URL)"

# On initial deploy, CLOUDFRONT_DOMAIN is empty, so Cognito won't have the
# CloudFront callback URL yet. It will be updated in Step 6.
deploy_stacks StratoclaveCognitoStack

log_ok "Cognito Stack deployed"

# --- Step 3: Verified Permissions Stack ---
log_step "Step 3/9: Deploying Verified Permissions Stack"

deploy_stacks StratoclaveVpStack

log_ok "Verified Permissions Stack deployed"

# --- Step 4: ECS Stack ---
log_step "Step 4/9: Deploying ECS Stack"

deploy_stacks StratoclaveEcsStack

log_ok "ECS Stack deployed"

# --- Step 5: Frontend Stack (S3 + CloudFront) ---
log_step "Step 5/9: Deploying Frontend Stack (S3 + CloudFront)"

deploy_stacks StratoclaveFrontendStack

log_ok "Frontend Stack deployed"

# --- Step 6: Retrieve CloudFront Domain and Update Cognito ---
log_step "Step 6/9: Updating Cognito with CloudFront callback URL"

CLOUDFRONT_DOMAIN=$(get_stack_output "StratoclaveFrontendStack" "CloudFrontDomainName") || {
  log_error "Cannot proceed without CloudFront domain name"
  exit 1
}
log_info "CloudFront Domain: $CLOUDFRONT_DOMAIN"

# Re-deploy Cognito with the CloudFront domain as callback URL
CLOUDFRONT_DOMAIN="$CLOUDFRONT_DOMAIN" deploy_stacks StratoclaveCognitoStack

log_ok "Cognito updated with callback URL: https://$CLOUDFRONT_DOMAIN/callback"

# --- Step 7: Frontend CodeBuild Stack ---
log_step "Step 7/9: Deploying Frontend CodeBuild Stack"

deploy_stacks StratoclaveFrontendCodeBuildStack

log_ok "Frontend CodeBuild Stack deployed"

# --- Step 8: Build and Deploy Frontend ---
log_step "Step 8/9: Building and deploying frontend assets"

if [ "$SKIP_BUILD" = true ]; then
  log_warn "Skipping frontend build (--skip-build)"
else
  # Retrieve configuration values from deployed stacks
  COGNITO_CLIENT_ID=$(get_stack_output "StratoclaveCognitoStack" "UserPoolClientId" "$COGNITO_REGION")
  COGNITO_USER_POOL_ID=$(get_stack_output "StratoclaveCognitoStack" "UserPoolId" "$COGNITO_REGION")
  COGNITO_DOMAIN=$(get_stack_output "StratoclaveCognitoStack" "CognitoDomain" "$COGNITO_REGION")
  ALB_DNS=$(get_stack_output "StratoclaveAlbStack" "AlbDnsName")
  FRONTEND_BUCKET=$(get_stack_output "StratoclaveFrontendStack" "FrontendBucketName")
  DISTRIBUTION_ID=$(get_stack_output "StratoclaveFrontendStack" "CloudFrontDistributionId")

  log_info "Cognito Client ID: $COGNITO_CLIENT_ID"
  log_info "Cognito User Pool: $COGNITO_USER_POOL_ID"
  log_info "Cognito Domain:    $COGNITO_DOMAIN"
  log_info "ALB DNS:           $ALB_DNS"
  log_info "Frontend Bucket:   $FRONTEND_BUCKET"
  log_info "Distribution ID:   $DISTRIBUTION_ID"

  # Create .env.production for frontend build
  log_info "Creating $FRONTEND_DIR/.env.production"
  cat > "$FRONTEND_DIR/.env.production" <<ENVEOF
VITE_COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID
VITE_COGNITO_DOMAIN=$COGNITO_DOMAIN
VITE_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
VITE_API_ENDPOINT=http://$ALB_DNS
VITE_CLOUDFRONT_URL=https://$CLOUDFRONT_DOMAIN
ENVEOF

  # Build frontend
  log_info "Building frontend..."
  cd "$FRONTEND_DIR"
  npm ci 2>&1 | tee -a "$DEPLOYMENT_LOG"
  npm run build 2>&1 | tee -a "$DEPLOYMENT_LOG"

  if [ ! -d "$FRONTEND_DIR/dist" ]; then
    log_error "Frontend build failed: dist/ directory not found"
    exit 1
  fi

  # Generate config.json for runtime (Cognito + API settings)
  log_info "Generating dist/config.json from CloudFormation outputs"
  cat > "$FRONTEND_DIR/dist/config.json" <<CONFIGEOF
{
  "cognito": {
    "user_pool_id": "$COGNITO_USER_POOL_ID",
    "client_id": "$COGNITO_CLIENT_ID",
    "domain": "https://$COGNITO_DOMAIN",
    "region": "$COGNITO_REGION"
  },
  "api": {
    "endpoint": ""
  },
  "app": {
    "cloudfront_domain": "$CLOUDFRONT_DOMAIN"
  }
}
CONFIGEOF

  # Upload to S3
  log_info "Uploading to S3 bucket: $FRONTEND_BUCKET"
  aws s3 sync "$FRONTEND_DIR/dist/" "s3://$FRONTEND_BUCKET/" \
    --delete \
    --region "$REGION" \
    2>&1 | tee -a "$DEPLOYMENT_LOG"

  # Invalidate CloudFront cache
  log_info "Invalidating CloudFront cache (Distribution: $DISTRIBUTION_ID)"
  aws cloudfront create-invalidation \
    --distribution-id "$DISTRIBUTION_ID" \
    --paths "/*" \
    --region "$REGION" \
    --output json 2>&1 | tee -a "$DEPLOYMENT_LOG"

  log_ok "Frontend built and deployed"
fi

# --- Step 9: Create Admin User (initial deploy only) ---
log_step "Step 9/9: Admin user setup"

COGNITO_USER_POOL_ID=$(get_stack_output "StratoclaveCognitoStack" "UserPoolId" "$COGNITO_REGION")

if cognito_user_exists "$COGNITO_USER_POOL_ID" "$ADMIN_EMAIL"; then
  log_info "Admin user $ADMIN_EMAIL already exists (skipping creation)"
else
  log_info "Creating admin user: $ADMIN_EMAIL"
  TEMP_PASSWORD="TempPass@$(date +%s | tail -c 5)"
  aws cognito-idp admin-create-user \
    --user-pool-id "$COGNITO_USER_POOL_ID" \
    --username "$ADMIN_EMAIL" \
    --user-attributes \
      Name=email,Value="$ADMIN_EMAIL" \
      Name=email_verified,Value=true \
    --temporary-password "$TEMP_PASSWORD" \
    --message-action SUPPRESS \
    --region "$COGNITO_REGION" \
    --output json 2>&1 | tee -a "$DEPLOYMENT_LOG"

  log_ok "Admin user created with temporary password"
  log_warn "Set a permanent password with:"
  echo ""
  echo "  aws cognito-idp admin-set-user-password \\"
  echo "    --user-pool-id $COGNITO_USER_POOL_ID \\"
  echo "    --username $ADMIN_EMAIL \\"
  echo "    --password 'YourSecurePassword@123' \\"
  echo "    --permanent \\"
  echo "    --region $COGNITO_REGION"
  echo ""
fi

# --- Summary ---
echo ""
echo "============================================"
echo "[SUCCESS] Deployment completed"
echo "============================================"
echo ""
echo "Deployed Resources:"
echo "  Frontend URL:  https://$CLOUDFRONT_DOMAIN"
echo "  ALB Endpoint:  http://$(get_stack_output 'StratoclaveAlbStack' 'AlbDnsName' 2>/dev/null || echo 'N/A')"
echo "  Cognito Domain: $(get_stack_output 'StratoclaveCognitoStack' 'CognitoDomain' "$COGNITO_REGION" 2>/dev/null || echo 'N/A')"
echo "  User Pool ID:  $COGNITO_USER_POOL_ID"
echo ""
echo "Admin User: $ADMIN_EMAIL"
echo ""
echo "Next Steps:"
echo "  1. Set admin password (see command above)"
echo "  2. Build and push backend Docker image:"
echo "     cd $IAC_DIR && ./scripts/cloud-build.sh"
echo "  3. Access the application:"
echo "     https://$CLOUDFRONT_DOMAIN"
echo ""
echo "Deployment log: $DEPLOYMENT_LOG"
echo ""
