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
    --ecr           Deploy ECR stack only
    --alb           Deploy ALB stack only
    --ecs           Deploy ECS stack only
    --frontend      Deploy Frontend stack only
    --help          Show this help message

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

# デフォルト設定
DEPLOY_ALL=true
DEPLOY_NETWORK=false
DEPLOY_ECR=false
DEPLOY_ALB=false
DEPLOY_ECS=false
DEPLOY_FRONTEND=false

# 引数解析
while [[ $# -gt 0 ]]; do
    case $1 in
        --all)
            DEPLOY_ALL=true
            shift
            ;;
        --network)
            DEPLOY_ALL=false
            DEPLOY_NETWORK=true
            shift
            ;;
        --ecr)
            DEPLOY_ALL=false
            DEPLOY_ECR=true
            shift
            ;;
        --alb)
            DEPLOY_ALL=false
            DEPLOY_ALB=true
            shift
            ;;
        --ecs)
            DEPLOY_ALL=false
            DEPLOY_ECS=true
            shift
            ;;
        --frontend)
            DEPLOY_ALL=false
            DEPLOY_FRONTEND=true
            shift
            ;;
        --help)
            usage
            ;;
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

# デプロイ関数
deploy_stack() {
    local stack_name=$1
    log_step "Deploying $stack_name..."
    npx cdk deploy $stack_name --require-approval never
}

# Frontendビルド関数
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

# デプロイ実行
if [ "$DEPLOY_ALL" = true ]; then
    log_info "Deploying all stacks..."
    deploy_stack StratoclaveNetworkStack
    deploy_stack StratoclaveEcrStack
    deploy_stack StratoclaveAlbStack
    deploy_stack StratoclaveEcsStack

    # Build frontend before deploying
    build_frontend
    deploy_stack StratoclaveFrontendStack
else
    if [ "$DEPLOY_NETWORK" = true ]; then
        deploy_stack StratoclaveNetworkStack
    fi
    if [ "$DEPLOY_ECR" = true ]; then
        deploy_stack StratoclaveEcrStack
    fi
    if [ "$DEPLOY_ALB" = true ]; then
        deploy_stack StratoclaveAlbStack
    fi
    if [ "$DEPLOY_ECS" = true ]; then
        deploy_stack StratoclaveEcsStack
    fi
    if [ "$DEPLOY_FRONTEND" = true ]; then
        build_frontend
        deploy_stack StratoclaveFrontendStack
    fi
fi

log_info "Deployment complete!"

# スタック出力を表示
log_step "Stack Outputs:"
ALB_DNS=$(aws cloudformation describe-stacks --stack-name StratoclaveAlbStack --query 'Stacks[0].Outputs[?OutputKey=="AlbDnsName"].OutputValue' --output text 2>/dev/null || echo "N/A")
FRONTEND_URL=$(aws cloudformation describe-stacks --stack-name StratoclaveFrontendStack --query 'Stacks[0].Outputs[?OutputKey=="FrontendUrl"].OutputValue' --output text 2>/dev/null || echo "N/A")

echo ""
echo "  Backend URL:  http://${ALB_DNS}"
echo "  Frontend URL: ${FRONTEND_URL}"
echo ""

log_info "Next steps:"
echo "  1. Build and push Docker image:"
echo "     ./scripts/build-and-push.sh"
echo "  2. Update Cognito Callback URLs:"
echo "     ./scripts/update-cognito-urls.sh"
echo "  3. Access the application:"
echo "     ${FRONTEND_URL}"
echo "  4. Wait 2-3 minutes for ECS tasks to start, then check:"
echo "     http://${ALB_DNS}/health"

# Auto-update Cognito URLs if frontend was deployed
if [ "$DEPLOY_ALL" = true ] || [ "$DEPLOY_FRONTEND" = true ]; then
    if [ -f "./scripts/update-cognito-urls.sh" ]; then
        echo ""
        log_info "Updating Cognito Callback URLs..."
        ./scripts/update-cognito-urls.sh || log_warn "Failed to update Cognito URLs. Run manually: ./scripts/update-cognito-urls.sh"
    fi
fi
