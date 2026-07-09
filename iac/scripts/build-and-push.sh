#!/bin/bash

# Build Docker image and push to ECR
set -e

# Colored log helpers
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
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

# Validate required arguments
if [ -z "$AWS_REGION" ]; then
    AWS_REGION="us-east-1"
    log_warn "AWS_REGION not set. Using default: $AWS_REGION"
fi

# Fetch ECR repository name
ECR_REPO_NAME=$(aws cloudformation describe-stacks \
    --stack-name StratoclaveEcrStack \
    --query 'Stacks[0].Outputs[?OutputKey==`RepositoryName`].OutputValue' \
    --output text \
    --region $AWS_REGION 2>/dev/null)

if [ -z "$ECR_REPO_NAME" ]; then
    log_error "ECR repository not found. Please deploy StratoclaveEcrStack first:"
    log_error "  cd iac && npx cdk deploy StratoclaveEcrStack"
    exit 1
fi

log_info "ECR Repository: $ECR_REPO_NAME"

# Retrieve AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

log_info "ECR URI: $ECR_URI"

# Authenticate to ECR
log_info "Logging in to ECR..."
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $ECR_URI

# Build Docker image
log_info "Building Docker image..."
cd "$(dirname "$0")/../../backend"
docker build -t stratoclave-backend:latest .

# Tag image
log_info "Tagging image..."
docker tag stratoclave-backend:latest $ECR_URI:latest
docker tag stratoclave-backend:latest $ECR_URI:$(date +%Y%m%d-%H%M%S)

# Push image to ECR
log_info "Pushing image to ECR..."
docker push $ECR_URI:latest
docker push $ECR_URI:$(date +%Y%m%d-%H%M%S)

log_info "Docker image pushed successfully!"
log_info "ECR URI: $ECR_URI:latest"
