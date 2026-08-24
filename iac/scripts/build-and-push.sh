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

# Container runtime. Defaults to docker; set CONTAINER_CLI to any drop-in CLI
# (finch, nerdctl, podman) so the documented path works on a host without docker
# rather than requiring an edit here.
CONTAINER_CLI="${CONTAINER_CLI:-docker}"
if ! command -v "$CONTAINER_CLI" >/dev/null 2>&1; then
    echo "[ERROR] container runtime '$CONTAINER_CLI' not found; set CONTAINER_CLI" >&2
    exit 1
fi

# Validate required arguments
if [ -z "$AWS_REGION" ]; then
    AWS_REGION="us-east-1"
    log_warn "AWS_REGION not set. Using default: $AWS_REGION"
fi

# Fetch ECR repository name from the ECR stack ("<prefix>-ecr"; prefix defaults
# to "stratoclave"). Falls back to describing the repo directly by its
# conventional name so a renamed/absent stack output does not block the push.
PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"
ECR_REPO_NAME=$(aws cloudformation describe-stacks \
    --stack-name "${PREFIX}-ecr" \
    --query 'Stacks[0].Outputs[?OutputKey==`RepositoryName`].OutputValue' \
    --output text \
    --region $AWS_REGION 2>/dev/null || true)

if [ -z "$ECR_REPO_NAME" ] || [ "$ECR_REPO_NAME" = "None" ]; then
    ECR_REPO_NAME=$(aws ecr describe-repositories \
        --repository-names "${PREFIX}-backend" \
        --query 'repositories[0].repositoryName' --output text \
        --region $AWS_REGION 2>/dev/null || true)
fi

if [ -z "$ECR_REPO_NAME" ] || [ "$ECR_REPO_NAME" = "None" ]; then
    log_error "ECR repository not found. Deploy the infrastructure first:"
    log_error "  cd iac && ./scripts/deploy-all.sh"
    exit 1
fi

log_info "ECR Repository: $ECR_REPO_NAME"

# Retrieve AWS account ID
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"

log_info "ECR URI: $ECR_URI"

# Authenticate to ECR
log_info "Logging in to ECR..."
aws ecr get-login-password --region $AWS_REGION | "$CONTAINER_CLI" login --username AWS --password-stdin $ECR_URI

# Build Docker image
log_info "Building image with $CONTAINER_CLI..."
cd "$(dirname "$0")/../../backend"
"$CONTAINER_CLI" build -t stratoclave-backend:latest .

# One timestamp for both the tag and the push: evaluating `date` twice can
# straddle a second boundary and push a tag that was never created.
BUILD_TAG="$(date +%Y%m%d-%H%M%S)"

# Tag image
log_info "Tagging image..."
"$CONTAINER_CLI" tag stratoclave-backend:latest $ECR_URI:latest
"$CONTAINER_CLI" tag stratoclave-backend:latest $ECR_URI:$BUILD_TAG

# Push image to ECR
log_info "Pushing image to ECR..."
"$CONTAINER_CLI" push $ECR_URI:latest
"$CONTAINER_CLI" push $ECR_URI:$BUILD_TAG

log_info "Docker image pushed successfully!"
log_info "ECR URI: $ECR_URI:latest"
