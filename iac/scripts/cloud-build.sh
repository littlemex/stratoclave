#!/bin/bash
# Cloud Build: backend コードを S3 にアップロードし、CodeBuild でビルド
set -euo pipefail

# 色付きログ
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_step()  { echo -e "${BLUE}[STEP]${NC} $1"; }

# 設定
AWS_REGION="${AWS_REGION:-us-east-1}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND_DIR="$PROJECT_ROOT/backend"
ARCHIVE_NAME="backend-source.tar.gz"
TMP_DIR=$(mktemp -d)

# クリーンアップ
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# 前提チェック
log_step "Checking prerequisites..."

if ! command -v aws &> /dev/null; then
    log_error "AWS CLI is not installed"
    exit 1
fi

if [ ! -d "$BACKEND_DIR" ]; then
    log_error "Backend directory not found: $BACKEND_DIR"
    exit 1
fi

if [ ! -f "$BACKEND_DIR/Dockerfile" ]; then
    log_error "Dockerfile not found: $BACKEND_DIR/Dockerfile"
    exit 1
fi

# CloudFormation から設定値を取得
log_step "Fetching stack outputs..."

S3_BUCKET=$(aws cloudformation describe-stacks \
    --stack-name StratoclaveCodeBuildStack \
    --query 'Stacks[0].Outputs[?OutputKey==`SourceBucketName`].OutputValue' \
    --output text \
    --region "$AWS_REGION" 2>/dev/null)

if [ -z "$S3_BUCKET" ] || [ "$S3_BUCKET" = "None" ]; then
    log_error "CodeBuild stack not found. Deploy it first:"
    log_error "  cd iac && npx cdk deploy StratoclaveCodeBuildStack"
    exit 1
fi

BUILD_PROJECT=$(aws cloudformation describe-stacks \
    --stack-name StratoclaveCodeBuildStack \
    --query 'Stacks[0].Outputs[?OutputKey==`BuildProjectName`].OutputValue' \
    --output text \
    --region "$AWS_REGION")

log_info "S3 Bucket: $S3_BUCKET"
log_info "CodeBuild Project: $BUILD_PROJECT"

# ソースコードのアーカイブ
log_step "Creating source archive..."

tar -czf "$TMP_DIR/$ARCHIVE_NAME" \
    -C "$BACKEND_DIR" \
    --exclude='venv' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.pytest_cache' \
    --exclude='htmlcov' \
    --exclude='.coverage' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='data' \
    --exclude='*.db' \
    --exclude='*.sqlite*' \
    --exclude='*.pyc' \
    .

ARCHIVE_SIZE=$(ls -lh "$TMP_DIR/$ARCHIVE_NAME" | awk '{print $5}')
log_info "Archive size: $ARCHIVE_SIZE"

# S3 にアップロード
log_step "Uploading source to S3..."

aws s3 cp "$TMP_DIR/$ARCHIVE_NAME" "s3://$S3_BUCKET/$ARCHIVE_NAME" \
    --region "$AWS_REGION" \
    --quiet

log_info "Source uploaded to s3://$S3_BUCKET/$ARCHIVE_NAME"

# CodeBuild ビルド開始
log_step "Starting CodeBuild build..."

BUILD_ID=$(aws codebuild start-build \
    --project-name "$BUILD_PROJECT" \
    --region "$AWS_REGION" \
    --query 'build.id' \
    --output text)

log_info "Build started: $BUILD_ID"
log_info "Console: https://${AWS_REGION}.console.aws.amazon.com/codesuite/codebuild/projects/${BUILD_PROJECT}/build/${BUILD_ID}/log"

# ビルド進捗のストリーミング
log_step "Waiting for build to complete (streaming logs)..."

PREV_PHASE=""
while true; do
    BUILD_STATUS=$(aws codebuild batch-get-builds \
        --ids "$BUILD_ID" \
        --region "$AWS_REGION" \
        --query 'builds[0].buildStatus' \
        --output text)

    CURRENT_PHASE=$(aws codebuild batch-get-builds \
        --ids "$BUILD_ID" \
        --region "$AWS_REGION" \
        --query 'builds[0].currentPhase' \
        --output text)

    if [ "$CURRENT_PHASE" != "$PREV_PHASE" ]; then
        log_info "Phase: $CURRENT_PHASE"
        PREV_PHASE="$CURRENT_PHASE"
    fi

    case "$BUILD_STATUS" in
        SUCCEEDED)
            echo ""
            log_info "Build SUCCEEDED"
            break
            ;;
        FAILED|FAULT|TIMED_OUT|STOPPED)
            echo ""
            log_error "Build $BUILD_STATUS"
            log_error "Check logs: aws logs tail /codebuild/stratoclave-backend --follow --region $AWS_REGION"
            exit 1
            ;;
        IN_PROGRESS)
            sleep 5
            ;;
        *)
            sleep 5
            ;;
    esac
done

# 完了
echo ""
log_info "Build and deploy completed successfully."
log_info "ECS service update has been triggered."
log_info "Monitor deployment: aws ecs describe-services --cluster stratoclave-cluster --services stratoclave-backend --query 'services[0].deployments' --region $AWS_REGION"
