#!/bin/bash

# CDK スタックのデプロイスクリプト
set -e

# 色付きログ
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

# 使い方
usage() {
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
    --all           Deploy all stacks (default)
    --network       Deploy Network stack only
    --dynamodb      Deploy DynamoDB stack only
    --ecr           Deploy ECR stack only
    --alb           Deploy ALB stack only
    --frontend      Deploy Frontend stack only (also rebuilds Frontend assets)
    --cognito       Deploy Cognito stack only
    --ecs           Deploy ECS stack only
    --config        Deploy BackendConfig (SSM Parameter Store) stack only
    --help          Show this help message

Note:
    For a full first-time deployment, prefer ./deploy-all.sh which also
    handles Frontend build + S3 sync + CloudFront invalidation in one pass.

Environment Variables:
    AWS_REGION      AWS region (default: us-east-1)
    AWS_PROFILE     AWS profile to use

Examples:
    # Deploy all stacks
    ./deploy.sh --all

    # Deploy specific stack
    ./deploy.sh --network

    # Use specific AWS profile
    AWS_PROFILE=myprofile ./deploy.sh --all
EOF
    exit 0
}

# Default: deploy everything
DEPLOY_ALL=true
TARGETS=()

# Argument parsing
while [[ $# -gt 0 ]]; do
    case $1 in
        --all)      DEPLOY_ALL=true; shift ;;
        --network)  DEPLOY_ALL=false; TARGETS+=("network"); shift ;;
        --dynamodb) DEPLOY_ALL=false; TARGETS+=("dynamodb"); shift ;;
        --ecr)      DEPLOY_ALL=false; TARGETS+=("ecr"); shift ;;
        --alb)      DEPLOY_ALL=false; TARGETS+=("alb"); shift ;;
        --frontend) DEPLOY_ALL=false; TARGETS+=("frontend"); shift ;;
        --cognito)  DEPLOY_ALL=false; TARGETS+=("cognito"); shift ;;
        --ecs)      DEPLOY_ALL=false; TARGETS+=("ecs"); shift ;;
        --config)   DEPLOY_ALL=false; TARGETS+=("config"); shift ;;
        --help)     usage ;;
        *)
            log_error "Unknown option: $1"
            usage
            ;;
    esac
done

# 環境変数のデフォルト設定
if [ -z "$AWS_REGION" ]; then
    AWS_REGION="us-east-1"
    log_warn "AWS_REGION not set. Using default: $AWS_REGION"
fi

log_info "AWS Region: $AWS_REGION"
if [ -n "$AWS_PROFILE" ]; then
    log_info "AWS Profile: $AWS_PROFILE"
fi

# CDK ディレクトリに移動
cd "$(dirname "$0")/.."

# npm install（初回のみ）
if [ ! -d "node_modules" ]; then
    log_step "Installing CDK dependencies..."
    npm install
fi

# CDK bootstrap（初回のみ）
log_step "Checking CDK bootstrap..."
if ! aws cloudformation describe-stacks --stack-name CDKToolkit --region $AWS_REGION &>/dev/null; then
    log_warn "CDK not bootstrapped in this region. Bootstrapping..."
    npx cdk bootstrap aws://$(aws sts get-caller-identity --query Account --output text)/$AWS_REGION
fi

# Map a short id to its CloudFormation stack name (matches iac/lib/_common.ts stackName()).
PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"

stack_name_for() {
    local id=$1
    echo "${PREFIX}-${id}"
}

deploy_stack() {
    local id=$1
    local stack_name
    stack_name=$(stack_name_for "$id")
    log_step "Deploying $stack_name..."
    npx cdk deploy "$stack_name" --require-approval never
}

build_frontend() {
    log_step "Building Frontend..."
    cd ../frontend
    if [ ! -d "node_modules" ]; then
        log_info "Installing frontend dependencies..."
        npm install
    fi
    log_info "Building frontend assets..."
    npm run build
    if [ ! -d "dist" ]; then
        log_error "Frontend build failed: dist directory not found"
        exit 1
    fi
    cd ../iac
    log_info "Frontend build complete"
}

# Stack dependency order (see iac/bin/iac.ts):
#   network → dynamodb → ecr → alb → frontend → cognito → ecs → config
if [ "$DEPLOY_ALL" = true ]; then
    log_info "Deploying all stacks..."
    npx cdk deploy --all --require-approval never
    build_frontend
else
    for target in "${TARGETS[@]}"; do
        if [ "$target" = "frontend" ]; then
            build_frontend
        fi
        deploy_stack "$target"
    done
fi

log_info "Deployment complete"

# Print stack outputs
log_step "Stack Outputs:"
ALB_DNS=$(aws cloudformation describe-stacks --stack-name "$(stack_name_for alb)" --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' --output text 2>/dev/null || echo "N/A")
CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks --stack-name "$(stack_name_for frontend)" --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontDomainName`].OutputValue' --output text 2>/dev/null || echo "N/A")

echo ""
echo "  Backend URL:   http://${ALB_DNS}"
echo "  Frontend URL:  https://${CLOUDFRONT_DOMAIN}"
echo ""

log_info "Next steps:"
echo "  1. Push the Backend container image:"
echo "     ./scripts/build-and-push.sh"
echo "  2. Create the first admin user (if this is the initial deploy):"
echo "     ../scripts/bootstrap-admin.sh --email admin@example.com"
echo "  3. Access the application:"
echo "     https://${CLOUDFRONT_DOMAIN}"
echo "  4. After ECS tasks start, health check:"
echo "     http://${ALB_DNS}/health"
