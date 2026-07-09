#!/bin/bash
set -e

#
# Post-Deployment Validation Script
#
# Runs automatically after CDK deployment to verify consistency between Cognito settings and config.json.
# This script should be executed at the end of a CI/CD pipeline or a manual deployment.
#

echo "[INFO] Post-Deployment Validation"
echo "=================================="

# AWS region
export AWS_REGION=${AWS_REGION:-us-east-1}

# Fetch Outputs from CloudFormation stacks
echo "[INFO] Fetching CloudFormation stack outputs..."

# Cognito stack
COGNITO_STACK_NAME="StratoclaveCognitoStack"
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "$COGNITO_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolId'].OutputValue" \
  --output text 2>/dev/null || echo "")

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "$COGNITO_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='UserPoolClientId'].OutputValue" \
  --output text 2>/dev/null || echo "")

# Frontend stack
FRONTEND_STACK_NAME="StratoclaveFrontendStack"
CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name "$FRONTEND_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDomainName'].OutputValue" \
  --output text 2>/dev/null || echo "")

FRONTEND_BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$FRONTEND_STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='FrontendBucketName'].OutputValue" \
  --output text 2>/dev/null || echo "")

# Validate required parameters
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

# URL for config.json
export CONFIG_S3_URL="https://${CLOUDFRONT_DOMAIN}/config.json"

# Validate Cognito configuration
export USER_POOL_ID
export CLIENT_ID
export CLOUDFRONT_DOMAIN
export CONFIG_S3_URL

# Run validate-cognito-config.sh
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
