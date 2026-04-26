#!/bin/bash
set -e

# Stratoclave Infrastructure Deployment Script
# This script ensures all stacks are deployed in the correct order
# to avoid CloudFormation Early Validation issues

echo "[INFO] Starting Stratoclave infrastructure deployment..."

# Set AWS region
export CDK_DEFAULT_REGION=${CDK_DEFAULT_REGION:-us-east-1}

# Step 0: Build frontend
echo "[INFO] Step 0/2: Building frontend..."
cd ../frontend
npm run build
cd ../iac

# Step 1: Deploy all infrastructure stacks (excluding Frontend)
echo "[INFO] Step 1/2: Deploying infrastructure stacks..."
npx cdk deploy \
  StratoclaveCognitoStack \
  StratoclaveNetworkStack \
  StratoclaveEcrStack \
  StratoclaveAlbStack \
  StratoclaveRdsStack \
  StratoclaveRedisStack \
  StratoclaveVpStack \
  StratoclaveWafStack \
  StratoclaveCodeBuildStack \
  StratoclaveEcsStack \
  --require-approval never \
  --concurrency 3

# Step 2: Get ALB DNS name from deployed stack
echo "[INFO] Getting ALB DNS name..."
export ALB_DNS_NAME=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveAlbStack \
  --query 'Stacks[0].Outputs[?OutputKey==`AlbDnsName`].OutputValue' \
  --output text \
  --region ${CDK_DEFAULT_REGION})

echo "[INFO] ALB DNS: ${ALB_DNS_NAME}"

# Step 3: Deploy Frontend stack with ALB DNS
echo "[INFO] Step 2/2: Deploying Frontend stack..."
npx cdk deploy StratoclaveFrontendStack --require-approval never

# Step 4: Invalidate CloudFront cache
echo "[INFO] Invalidating CloudFront cache..."
DISTRIBUTION_ID=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontDistributionId`].OutputValue' \
  --output text \
  --region ${CDK_DEFAULT_REGION})

if [ -n "$DISTRIBUTION_ID" ]; then
  echo "[INFO] CloudFront Distribution ID: ${DISTRIBUTION_ID}"
  aws cloudfront create-invalidation \
    --distribution-id ${DISTRIBUTION_ID} \
    --paths "/*" \
    --region ${CDK_DEFAULT_REGION} \
    --no-cli-pager > /dev/null 2>&1 || echo "[WARN] Cache invalidation failed (non-critical)"
fi

echo "[INFO] All stacks deployed successfully!"

# Display outputs
echo ""
echo "=== Deployment Summary ==="
npx cdk outputs StratoclaveFrontendStack --require-approval never 2>/dev/null || true
npx cdk outputs StratoclaveEcsStack --require-approval never 2>/dev/null || true
npx cdk outputs StratoclaveCognitoStack --require-approval never 2>/dev/null || true

echo ""
echo "[INFO] Deployment complete. Check outputs above for URLs and IDs."
