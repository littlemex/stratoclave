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

# Build Docker image. Fargate is x86_64 by default; a build host running
# arm64 (Apple Silicon) silently produces an arm64 image otherwise, which
# only surfaces later as "exec /app/entrypoint.sh: exec format error" in the
# ECS task's CloudWatch logs. PLATFORM defaults to the same linux/amd64 the
# Lambda image build below already uses, so one variable controls both.
PLATFORM="${PLATFORM:-linux/amd64}"
log_info "Building image with $CONTAINER_CLI for $PLATFORM..."
cd "$(dirname "$0")/../../backend"
"$CONTAINER_CLI" build --platform "$PLATFORM" -t stratoclave-backend:latest .

# One timestamp for both the tag and the push: evaluating `date` twice can
# straddle a second boundary and push a tag that was never created.
BUILD_TAG="$(date +%Y%m%d-%H%M%S)"

# Tag image
log_info "Tagging image..."
"$CONTAINER_CLI" tag stratoclave-backend:latest $ECR_URI:latest
"$CONTAINER_CLI" tag stratoclave-backend:latest $ECR_URI:$BUILD_TAG

# The repository's imageTagMutability is IMMUTABLE, so re-pushing ":latest"
# after it already exists is rejected outright ("400 Bad Request") rather
# than overwriting it — the one AWS-documented way past that on an immutable
# repository is to delete the existing tag first. Best-effort: a first-ever
# push has no ":latest" to delete, so a failure here is not fatal.
aws ecr batch-delete-image --repository-name "$ECR_REPO_NAME" \
    --image-ids imageTag=latest --region "$AWS_REGION" >/dev/null 2>&1 || true

# Push image to ECR
log_info "Pushing image to ECR..."
"$CONTAINER_CLI" push $ECR_URI:latest
"$CONTAINER_CLI" push $ECR_URI:$BUILD_TAG

log_info "Docker image pushed successfully!"
log_info "ECR URI: $ECR_URI:latest"

# --- Lambda image (backend/Dockerfile.lambda) --------------------------------
# The scheduled jobs (ledger projector/reconciler, certificate issuer, quota
# reconciler/period-rollover, quota grant sweeper) run a SEPARATE image built
# from backend/Dockerfile.lambda: it bakes in the AWS Lambda Runtime Interface
# Client, which the uvicorn image above does not have and will not run under.
# Nothing before this point built or pushed it, so following only the steps
# above leaves every one of those Lambda functions pointing at either a
# nonexistent tag or, worse, the ECS backend's own image.
#
# It shares this repository with the ECS backend image (a second repository
# was considered and rejected: every one of the four scheduled-job stacks
# already takes `lambdaRepository` as the SAME ecrStack.repository, and
# splitting it would touch stacks outside this fix, wire a second repository
# through all of them, and still not by itself stop a mistagged push — a
# distinct tag NAMESPACE does that with a one-line change). It MUST NOT share
# a TAG: iac/bin/iac.ts fails synth if LAMBDA_IMAGE_TAG equals the backend's
# own IMAGE_TAG, because the ECR repository's IMMUTABLE policy only blocks
# re-pushing an EXISTING tag — it does not stop two different images from
# being pushed under the same tag one after another, which would silently
# retag whichever image ECS is currently running.
#
# Skippable with BUILD_LAMBDA_IMAGE=false for a backend-only re-deploy that
# does not touch any scheduled-job code.
BUILD_LAMBDA_IMAGE="${BUILD_LAMBDA_IMAGE:-true}"
if [ "$BUILD_LAMBDA_IMAGE" = "true" ]; then
    # Same reasoning as the platform note on the backend image above: the
    # Lambda Runtime Interface Client binary is architecture-specific too, and
    # a Lambda function defaults to x86_64.
    PLATFORM="${PLATFORM:-linux/amd64}"
    LAMBDA_IMAGE_TAG="lambda-${BUILD_TAG}"

    log_info "Building Lambda image (backend/Dockerfile.lambda) for $PLATFORM..."
    "$CONTAINER_CLI" build --platform "$PLATFORM" -f Dockerfile.lambda -t stratoclave-backend-lambda:latest .

    log_info "Tagging Lambda image..."
    "$CONTAINER_CLI" tag stratoclave-backend-lambda:latest "$ECR_URI:$LAMBDA_IMAGE_TAG"

    log_info "Pushing Lambda image to ECR..."
    "$CONTAINER_CLI" push "$ECR_URI:$LAMBDA_IMAGE_TAG"

    log_info "Lambda image pushed successfully!"
    log_info "ECR URI: $ECR_URI:$LAMBDA_IMAGE_TAG"
    log_info "Export this before deploying the quota-reconciler / quota-grants stacks:"
    log_info "  export LAMBDA_IMAGE_TAG=$LAMBDA_IMAGE_TAG"
else
    log_warn "Skipping Lambda image build (BUILD_LAMBDA_IMAGE=false)"
fi
