#!/bin/bash
set -e

#
# Post-Deployment Validation Script
#
# CDK デプロイ後に自動実行し、Cognito 設定と config.json の整合性を確認します。
# このスクリプトは CI/CD パイプラインや手動デプロイの最後に実行されるべきです。
#

echo "[INFO] Post-Deployment Validation"
echo "=================================="

# AWS リージョン
export AWS_REGION=${AWS_REGION:-us-east-1}

# CloudFormation スタックから Outputs を取得
echo "[INFO] Fetching CloudFormation stack outputs..."

# Cognito Stack
COGNITO_STACK_NAME="StratoclaveCognitoStack"
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "$COGNITO_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text 2>/dev/null || echo "")

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "$COGNITO_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
  --output text 2>/dev/null || echo "")

# Frontend Stack
FRONTEND_STACK_NAME="StratoclaveFrontendStack"
CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name "$FRONTEND_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" \
  --output text 2>/dev/null || echo "")

FRONTEND_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$FRONTEND_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text 2>/dev/null || echo "")

# 必須パラメータのチェック
if [ -z "$USER_POOL_ID" ]; then
  echo "[ERROR] USER_POOL_ID not found in CloudFormation outputs"
  exit 1
fi

if [ -z "$CLIENT_ID" ]; then
  echo "[ERROR] CLIENT_ID not found in CloudFormation outputs"
  exit 1
fi

if [ -z "$CLOUDFRONT_DOMAIN" ]; then
  echo "[ERROR] CLOUDFRONT_DOMAIN not found in CloudFormation outputs"
  exit 1
fi

if [ -z "$FRONTEND_BUCKET" ]; then
  echo "[ERROR] FRONTEND_BUCKET not found in CloudFormation outputs"
  exit 1
fi

echo "[INFO] USER_POOL_ID: $USER_POOL_ID"
echo "[INFO] CLIENT_ID: $CLIENT_ID"
echo "[INFO] CLOUDFRONT_DOMAIN: $CLOUDFRONT_DOMAIN"
echo "[INFO] FRONTEND_BUCKET: $FRONTEND_BUCKET"
echo ""

# config.json の URL
export CONFIG_S3_URL="https://${CLOUDFRONT_DOMAIN}/config.json"

# Cognito 設定の検証
export USER_POOL_ID
export CLIENT_ID
export CLOUDFRONT_DOMAIN
export CONFIG_S3_URL

# validate-cognito-config.sh を実行
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$SCRIPT_DIR/validate-cognito-config.sh"

if [ $? -ne 0 ]; then
  echo ""
  echo "[ERROR] Cognito configuration validation failed"
  echo "[ERROR] Please check the following:"
  echo "  1. User Pool Client CallbackURLs include: https://${CLOUDFRONT_DOMAIN}/callback"
  echo "  2. User Pool Client LogoutURLs include: https://${CLOUDFRONT_DOMAIN}"
  echo "  3. config.json matches Cognito settings"
  echo ""
  echo "[FIX] To fix this, update CognitoStack with cloudFrontDomainName:"
  echo "  const cognitoStack = new CognitoStack(app, 'StratoclaveCognitoStack', {"
  echo "    cloudFrontDomainName: '${CLOUDFRONT_DOMAIN}',"
  echo "  })"
  echo "  Then run: npx cdk deploy StratoclaveCognitoStack"
  exit 1
fi

echo ""
echo "[SUCCESS] Post-deployment validation passed"
echo "=================================="
