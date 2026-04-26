#!/bin/bash

# Update Cognito Callback URLs with CloudFront domain
# Cognito User Pool ID and Client ID are retrieved dynamically from
# the StratoclaveCognitoStack CloudFormation outputs.

set -e

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}Updating Cognito Callback URLs...${NC}"

# Cognito region (Cognito is deployed in us-east-1)
COGNITO_REGION="${COGNITO_REGION:-us-east-1}"

# ---------------------------------------------------------------------------
# 1. Get CloudFront domain from FrontendStack
# ---------------------------------------------------------------------------
FRONTEND_URL=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`FrontendUrl`].OutputValue' \
  --output text 2>/dev/null)

if [ -z "$FRONTEND_URL" ] || [ "$FRONTEND_URL" = "None" ]; then
  echo -e "${RED}Error: Could not get Frontend URL from CloudFormation${NC}"
  exit 1
fi

CALLBACK_URL="${FRONTEND_URL}/callback"
LOGOUT_URL="${FRONTEND_URL}"

echo -e "${YELLOW}Frontend URL: ${FRONTEND_URL}${NC}"
echo -e "${YELLOW}Callback URL: ${CALLBACK_URL}${NC}"

# ---------------------------------------------------------------------------
# 2. Get Cognito User Pool ID and Client ID from CognitoStack
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Retrieving Cognito configuration from StratoclaveCognitoStack...${NC}"

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveCognitoStack \
  --region "${COGNITO_REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text 2>/dev/null || true)

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveCognitoStack \
  --region "${COGNITO_REGION}" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text 2>/dev/null || true)

if [ -z "$USER_POOL_ID" ] || [ "$USER_POOL_ID" = "None" ]; then
  echo -e "${RED}Error: Could not get UserPoolId from StratoclaveCognitoStack (region: ${COGNITO_REGION})${NC}"
  echo "Ensure the Cognito stack is deployed and exports 'UserPoolId' as a CfnOutput."
  exit 1
fi

if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "None" ]; then
  echo -e "${RED}Error: Could not get UserPoolClientId from StratoclaveCognitoStack (region: ${COGNITO_REGION})${NC}"
  echo "Ensure the Cognito stack is deployed and exports 'UserPoolClientId' as a CfnOutput."
  exit 1
fi

echo -e "${YELLOW}User Pool ID: ${USER_POOL_ID}${NC}"
echo -e "${YELLOW}Client ID:    ${CLIENT_ID}${NC}"

# ---------------------------------------------------------------------------
# 3. Update User Pool Client callback / logout URLs
# ---------------------------------------------------------------------------
aws cognito-idp update-user-pool-client \
  --user-pool-id "${USER_POOL_ID}" \
  --client-id "${CLIENT_ID}" \
  --region "${COGNITO_REGION}" \
  --callback-urls \
    "http://127.0.0.1:18080/callback" \
    "http://localhost:3003/callback" \
    "${CALLBACK_URL}" \
  --logout-urls \
    "http://127.0.0.1:18080" \
    "http://localhost:3003" \
    "${LOGOUT_URL}" \
  --allowed-o-auth-flows "code" \
  --allowed-o-auth-scopes "openid" "email" "profile" \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers "COGNITO" \
  --output json > /dev/null

echo -e "${GREEN}Cognito Callback URLs updated successfully!${NC}"
echo
echo "Updated URLs:"
echo "  - http://127.0.0.1:18080/callback"
echo "  - http://localhost:3003/callback"
echo "  - ${CALLBACK_URL}"

exit 0
