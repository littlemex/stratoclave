#!/bin/bash
set -e

#
# Cognito Configuration Validator
#
# This script checks the following:
# 1. Whether CloudFront URL is included in User Pool Client CallbackURLs
# 2. Whether CloudFront URL is included in User Pool Client LogoutURLs
# 3. Whether User Pool Domain is correct
# 4. Whether config.json cognito.domain matches User Pool Domain
# 5. Whether config.json cognito.client_id is valid
#

echo "[INFO] Cognito Configuration Validator"
echo "========================================"

# Validate environment variables
if [ -z "$USER_POOL_ID" ]; then
  echo "[ERROR] USER_POOL_ID is not set"
  exit 1
fi

if [ -z "$CLIENT_ID" ]; then
  echo "[ERROR] CLIENT_ID is not set"
  exit 1
fi

if [ -z "$CLOUDFRONT_DOMAIN" ]; then
  echo "[ERROR] CLOUDFRONT_DOMAIN is not set"
  exit 1
fi

echo "[INFO] USER_POOL_ID: $USER_POOL_ID"
echo "[INFO] CLIENT_ID: $CLIENT_ID"
echo "[INFO] CLOUDFRONT_DOMAIN: $CLOUDFRONT_DOMAIN"
echo ""

# Fetch User Pool Client configuration
echo "[CHECK 1] Fetching User Pool Client configuration..."
CLIENT_CONFIG=$(aws cognito-idp describe-user-pool-client \
  --user-pool-id "$USER_POOL_ID" \
  --client-id "$CLIENT_ID" \
  --output json)

if [ $? -ne 0 ]; then
  echo "[ERROR] Failed to fetch User Pool Client"
  exit 1
fi

# Validate CallbackURLs
echo "[CHECK 2] Validating CallbackURLs..."
CALLBACK_URLS=$(echo "$CLIENT_CONFIG" | jq -r '.UserPoolClient.CallbackURLs[]')
CLOUDFRONT_CALLBACK="https://${CLOUDFRONT_DOMAIN}/callback"

if echo "$CALLBACK_URLS" | grep -q "$CLOUDFRONT_CALLBACK"; then
  echo "[OK] CloudFront callback URL is registered: $CLOUDFRONT_CALLBACK"
else
  echo "[ERROR] CloudFront callback URL is NOT registered: $CLOUDFRONT_CALLBACK"
  echo "[ERROR] Registered CallbackURLs:"
  echo "$CALLBACK_URLS"
  exit 1
fi

# Validate LogoutURLs
echo "[CHECK 3] Validating LogoutURLs..."
LOGOUT_URLS=$(echo "$CLIENT_CONFIG" | jq -r '.UserPoolClient.LogoutURLs[]')
CLOUDFRONT_LOGOUT="https://${CLOUDFRONT_DOMAIN}"

if echo "$LOGOUT_URLS" | grep -q "$CLOUDFRONT_LOGOUT"; then
  echo "[OK] CloudFront logout URL is registered: $CLOUDFRONT_LOGOUT"
else
  echo "[ERROR] CloudFront logout URL is NOT registered: $CLOUDFRONT_LOGOUT"
  echo "[ERROR] Registered LogoutURLs:"
  echo "$LOGOUT_URLS"
  exit 1
fi

# Validate User Pool Domain
echo "[CHECK 4] Validating User Pool Domain..."
USER_POOL_DOMAIN=$(aws cognito-idp describe-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --query 'UserPool.Domain' \
  --output text)

if [ -z "$USER_POOL_DOMAIN" ]; then
  echo "[ERROR] User Pool Domain is not configured"
  exit 1
fi

echo "[INFO] User Pool Domain: $USER_POOL_DOMAIN"
EXPECTED_COGNITO_DOMAIN="https://${USER_POOL_DOMAIN}.auth.${AWS_REGION}.amazoncognito.com"

# Validate config.json (fetched from S3)
if [ -n "$CONFIG_S3_URL" ]; then
  echo "[CHECK 5] Validating config.json from S3..."
  CONFIG_JSON=$(curl -s "$CONFIG_S3_URL")

  if [ $? -ne 0 ]; then
    echo "[ERROR] Failed to fetch config.json from $CONFIG_S3_URL"
    exit 1
  fi

  CONFIG_CLIENT_ID=$(echo "$CONFIG_JSON" | jq -r '.cognito.client_id')
  CONFIG_DOMAIN=$(echo "$CONFIG_JSON" | jq -r '.cognito.domain')

  # Validate client_id
  if [ "$CONFIG_CLIENT_ID" != "$CLIENT_ID" ]; then
    echo "[ERROR] config.json client_id mismatch"
    echo "[ERROR] Expected: $CLIENT_ID"
    echo "[ERROR] Got: $CONFIG_CLIENT_ID"
    exit 1
  fi

  # Validate domain
  if [ "$CONFIG_DOMAIN" != "$EXPECTED_COGNITO_DOMAIN" ]; then
    echo "[ERROR] config.json cognito domain mismatch"
    echo "[ERROR] Expected: $EXPECTED_COGNITO_DOMAIN"
    echo "[ERROR] Got: $CONFIG_DOMAIN"
    exit 1
  fi

  echo "[OK] config.json is valid"
fi

# Validate AllowedOAuthFlows
echo "[CHECK 6] Validating OAuth flows..."
OAUTH_FLOWS=$(echo "$CLIENT_CONFIG" | jq -r '.UserPoolClient.AllowedOAuthFlows[]')

if echo "$OAUTH_FLOWS" | grep -q "code"; then
  echo "[OK] Authorization code flow is enabled"
else
  echo "[ERROR] Authorization code flow is NOT enabled"
  exit 1
fi

# Validate AllowedOAuthScopes
echo "[CHECK 7] Validating OAuth scopes..."
OAUTH_SCOPES=$(echo "$CLIENT_CONFIG" | jq -r '.UserPoolClient.AllowedOAuthScopes[]')

REQUIRED_SCOPES=("openid" "email" "profile")
for scope in "${REQUIRED_SCOPES[@]}"; do
  if echo "$OAUTH_SCOPES" | grep -q "$scope"; then
    echo "[OK] Scope '$scope' is enabled"
  else
    echo "[ERROR] Scope '$scope' is NOT enabled"
    exit 1
  fi
done

echo ""
echo "[SUCCESS] All checks passed"
echo "========================================"
