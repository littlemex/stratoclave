#!/bin/bash
set -euo pipefail

###############################################################################
# Stratoclave Frontend Deployment Script
#
# Builds the React frontend locally and deploys to S3 + invalidates CloudFront.
# All configuration values are automatically retrieved from CloudFormation
# stack outputs.
#
# Usage:
#   ./deploy-frontend.sh              # Build locally and deploy to S3
#   ./deploy-frontend.sh --codebuild  # Package and trigger CodeBuild instead
#   ./deploy-frontend.sh --skip-build # Upload existing dist/ without rebuilding
#
# Environment Variables:
#   CDK_DEFAULT_REGION  - AWS region (default: us-east-1)
#   AWS_PROFILE         - AWS profile to use
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$IAC_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
REGION="${CDK_DEFAULT_REGION:-us-east-1}"
COGNITO_REGION="us-east-1"

USE_CODEBUILD=false
SKIP_BUILD=false

# --- Argument Parsing ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --codebuild)
      USE_CODEBUILD=true
      shift
      ;;
    --skip-build)
      SKIP_BUILD=true
      shift
      ;;
    --help)
      echo "Usage: $0 [OPTIONS]"
      echo ""
      echo "Options:"
      echo "  --codebuild   Package source and trigger CodeBuild (recommended for production)"
      echo "  --skip-build  Upload existing dist/ directory without rebuilding"
      echo "  --help        Show this help message"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1"
      exit 1
      ;;
  esac
done

# --- Helpers ---
log_info()  { echo "[INFO]  $1"; }
log_step()  { echo ""; echo "[STEP]  $1"; echo "---"; }
log_error() { echo "[ERROR] $1" >&2; }
log_ok()    { echo "[OK]    $1"; }

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

# --- Pre-flight Checks ---
log_step "Pre-flight Checks"

if ! command -v aws &>/dev/null; then
  log_error "AWS CLI is required but not installed."
  exit 1
fi

aws sts get-caller-identity --output text >/dev/null 2>&1 || {
  log_error "AWS credentials not configured."
  exit 1
}

if ! command -v jq &>/dev/null; then
  log_error "jq is required but not installed. Install with: brew install jq"
  exit 1
fi

# --- CodeBuild Mode ---
if [ "$USE_CODEBUILD" = true ]; then
  log_step "CodeBuild Mode: Packaging and triggering remote build"

  SOURCE_BUCKET=$(get_stack_output "StratoclaveFrontendCodeBuildStack" "FrontendSourceBucketName") || {
    log_error "StratoclaveFrontendCodeBuildStack not deployed. Deploy it first."
    exit 1
  }
  BUILD_PROJECT=$(get_stack_output "StratoclaveFrontendCodeBuildStack" "FrontendBuildProjectName") || {
    log_error "Cannot retrieve CodeBuild project name."
    exit 1
  }

  log_info "Source Bucket:  $SOURCE_BUCKET"
  log_info "Build Project:  $BUILD_PROJECT"

  # Create temporary directory
  TMP_DIR=$(mktemp -d)
  trap "rm -rf $TMP_DIR" EXIT

  # Package frontend source
  log_info "Packaging frontend source..."
  cd "$PROJECT_ROOT"
  tar -czf "$TMP_DIR/frontend-source.tar.gz" \
    --exclude='node_modules' \
    --exclude='dist' \
    --exclude='.env*' \
    --exclude='.vite' \
    frontend/

  # Upload to S3
  log_info "Uploading to S3..."
  aws s3 cp "$TMP_DIR/frontend-source.tar.gz" "s3://$SOURCE_BUCKET/frontend-source.tar.gz" \
    --region "$REGION"

  # Trigger CodeBuild
  log_info "Triggering CodeBuild..."
  BUILD_ID=$(aws codebuild start-build \
    --project-name "$BUILD_PROJECT" \
    --region "$REGION" \
    --query 'build.id' \
    --output text)

  log_ok "Build started: $BUILD_ID"
  echo ""
  echo "Monitor build progress:"
  echo "  aws codebuild batch-get-builds --ids $BUILD_ID --region $REGION"
  echo "  aws logs tail /codebuild/stratoclave-frontend --follow --region $REGION"
  exit 0
fi

# --- Local Build Mode ---
log_step "Retrieving configuration from CloudFormation"

COGNITO_CLIENT_ID=$(get_stack_output "StratoclaveCognitoStack" "UserPoolClientId" "$COGNITO_REGION")
COGNITO_USER_POOL_ID=$(get_stack_output "StratoclaveCognitoStack" "UserPoolId" "$COGNITO_REGION")
COGNITO_DOMAIN=$(get_stack_output "StratoclaveCognitoStack" "CognitoDomain" "$COGNITO_REGION")
ALB_DNS=$(get_stack_output "StratoclaveAlbStack" "AlbDnsName")
CLOUDFRONT_DOMAIN=$(get_stack_output "StratoclaveFrontendStack" "CloudFrontDomainName")
FRONTEND_BUCKET=$(get_stack_output "StratoclaveFrontendStack" "FrontendBucketName")
DISTRIBUTION_ID=$(get_stack_output "StratoclaveFrontendStack" "CloudFrontDistributionId")

log_info "Cognito Client ID: $COGNITO_CLIENT_ID"
log_info "Cognito User Pool: $COGNITO_USER_POOL_ID"
log_info "Cognito Domain:    $COGNITO_DOMAIN"
log_info "ALB DNS:           $ALB_DNS"
log_info "CloudFront Domain: $CLOUDFRONT_DOMAIN"
log_info "Frontend Bucket:   $FRONTEND_BUCKET"
log_info "Distribution ID:   $DISTRIBUTION_ID"

if [ "$SKIP_BUILD" = false ]; then
  # Create .env.production
  log_step "Building Frontend"

  log_info "Creating .env.production"
  cat > "$FRONTEND_DIR/.env.production" <<ENVEOF
VITE_COGNITO_CLIENT_ID=$COGNITO_CLIENT_ID
VITE_COGNITO_DOMAIN=$COGNITO_DOMAIN
VITE_COGNITO_USER_POOL_ID=$COGNITO_USER_POOL_ID
VITE_API_ENDPOINT=http://$ALB_DNS
VITE_CLOUDFRONT_URL=https://$CLOUDFRONT_DOMAIN
ENVEOF

  # Build
  cd "$FRONTEND_DIR"
  log_info "Installing dependencies..."
  npm ci
  log_info "Building..."
  npm run build

  if [ ! -d "$FRONTEND_DIR/dist" ]; then
    log_error "Build failed: dist/ directory not found"
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

  log_ok "Frontend build completed"
else
  if [ ! -d "$FRONTEND_DIR/dist" ]; then
    log_error "dist/ directory not found. Run without --skip-build first."
    exit 1
  fi
  log_info "Using existing dist/ directory (--skip-build)"
fi

# Upload to S3
log_step "Deploying to S3"

log_info "Syncing to s3://$FRONTEND_BUCKET/"
aws s3 sync "$FRONTEND_DIR/dist/" "s3://$FRONTEND_BUCKET/" \
  --delete \
  --region "$REGION"

log_ok "S3 upload completed"

# Invalidate CloudFront
log_step "Invalidating CloudFront Cache"

INVALIDATION_ID=$(aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION_ID" \
  --paths "/*" \
  --region "$REGION" \
  --query 'Invalidation.Id' \
  --output text)

log_ok "Invalidation created: $INVALIDATION_ID"

# Summary
echo ""
echo "============================================"
echo "[SUCCESS] Frontend deployment completed"
echo "============================================"
echo ""
echo "URL: https://$CLOUDFRONT_DOMAIN"
echo ""
echo "Note: CloudFront cache invalidation may take 1-2 minutes to propagate."
echo "Monitor invalidation:"
echo "  aws cloudfront get-invalidation --distribution-id $DISTRIBUTION_ID --id $INVALIDATION_ID --region $REGION"
echo ""
