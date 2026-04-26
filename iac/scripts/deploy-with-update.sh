#!/bin/bash

# Two-Stage Deploy Script
#
# Solves the CloudFront chicken-egg problem:
#   - Cognito needs CloudFront domain for callback URLs
#   - CloudFront domain is only known after FrontendStack deploys
#   - On first deploy, iac.ts has a placeholder CloudFront domain
#
# Stage 1: Deploy all stacks (Cognito gets placeholder callback URL)
# Stage 2: Read actual CloudFront domain, update Cognito callback URLs
# Stage 3: Update iac.ts and outputs.json with actual values
# Stage 4: Re-validate configuration
#
# Usage:
#   ./scripts/deploy-with-update.sh                    # Full deploy
#   ./scripts/deploy-with-update.sh --stage2-only      # Only update Cognito URLs (post-deploy fix)
#   ./scripts/deploy-with-update.sh --dry-run          # Show what would be done
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - CDK bootstrapped in target account/region
#   - Node.js and npm installed

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IAC_TS="$IAC_DIR/bin/iac.ts"
OUTPUTS_FILE="$IAC_DIR/outputs.json"

# Cognito is deployed in us-east-1
COGNITO_REGION="${COGNITO_REGION:-us-east-1}"

# Parse arguments
STAGE2_ONLY=false
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --stage2-only) STAGE2_ONLY=true ;;
    --dry-run) DRY_RUN=true ;;
    --help|-h)
      echo "Usage: $0 [--stage2-only] [--dry-run]"
      echo "  --stage2-only  Skip CDK deploy, only update Cognito callback URLs"
      echo "  --dry-run      Show what would be done without making changes"
      exit 0
      ;;
  esac
done

echo "================================================================"
echo " Stratoclave Two-Stage Deploy"
echo "================================================================"
echo ""

# ============================================================================
# Stage 1: CDK Deploy (all stacks)
# ============================================================================
if [ "$STAGE2_ONLY" = false ]; then
  echo "=== Stage 1: CDK Deploy ==="
  echo "[INFO] Deploying all stacks..."
  echo "[INFO] Note: Cognito callback URLs will use the placeholder CloudFront domain"
  echo "[INFO]       from iac.ts. They will be corrected in Stage 2."
  echo ""

  if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Would run: cd $IAC_DIR && cdk deploy --all --outputs-file outputs.json --require-approval never"
  else
    cd "$IAC_DIR"
    cdk deploy --all --outputs-file outputs.json --require-approval never
  fi

  echo ""
  echo "[OK] Stage 1 complete."
  echo ""
fi

# ============================================================================
# Stage 2: Update Cognito Callback URLs with actual CloudFront domain
# ============================================================================
echo "=== Stage 2: Update Cognito Callback URLs ==="

# 2a. Get actual CloudFront domain
echo "[INFO] Retrieving actual CloudFront domain from StratoclaveFrontendStack..."

ACTUAL_CF_DOMAIN=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveFrontendStack \
  --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontDomainName`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -z "$ACTUAL_CF_DOMAIN" ] || [ "$ACTUAL_CF_DOMAIN" = "None" ]; then
  echo "[ERROR] Could not retrieve CloudFront domain from StratoclaveFrontendStack."
  echo "        Ensure the Frontend stack has been deployed."
  exit 1
fi

echo "[INFO] Actual CloudFront domain: $ACTUAL_CF_DOMAIN"

# 2b. Get Cognito User Pool ID and Client ID
echo "[INFO] Retrieving Cognito configuration..."

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveCognitoStack \
  --region "$COGNITO_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text 2>/dev/null || echo "")

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name StratoclaveCognitoStack \
  --region "$COGNITO_REGION" \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolClientId`].OutputValue' \
  --output text 2>/dev/null || echo "")

if [ -z "$USER_POOL_ID" ] || [ "$USER_POOL_ID" = "None" ]; then
  echo "[ERROR] Could not retrieve User Pool ID from StratoclaveCognitoStack."
  exit 1
fi

if [ -z "$CLIENT_ID" ] || [ "$CLIENT_ID" = "None" ]; then
  echo "[ERROR] Could not retrieve Client ID from StratoclaveCognitoStack."
  exit 1
fi

echo "[INFO] User Pool ID: $USER_POOL_ID"
echo "[INFO] Client ID: $CLIENT_ID"

# 2c. Update Cognito callback/logout URLs
CALLBACK_URLS=(
  "http://127.0.0.1:18080/callback"
  "http://localhost:3003/callback"
  "http://localhost:3004/callback"
  "http://localhost:5173/callback"
  "https://${ACTUAL_CF_DOMAIN}/callback"
)

LOGOUT_URLS=(
  "http://127.0.0.1:18080"
  "http://localhost:3003"
  "http://localhost:3004"
  "http://localhost:5173"
  "https://${ACTUAL_CF_DOMAIN}"
)

echo "[INFO] Updating Cognito User Pool Client callback URLs..."
echo "[INFO] Callback URLs:"
for url in "${CALLBACK_URLS[@]}"; do
  echo "  - $url"
done
echo "[INFO] Logout URLs:"
for url in "${LOGOUT_URLS[@]}"; do
  echo "  - $url"
done

if [ "$DRY_RUN" = true ]; then
  echo "[DRY-RUN] Would update Cognito User Pool Client with above URLs"
else
  aws cognito-idp update-user-pool-client \
    --user-pool-id "$USER_POOL_ID" \
    --client-id "$CLIENT_ID" \
    --region "$COGNITO_REGION" \
    --callback-urls "${CALLBACK_URLS[@]}" \
    --logout-urls "${LOGOUT_URLS[@]}" \
    --allowed-o-auth-flows "code" \
    --allowed-o-auth-scopes "openid" "email" "profile" \
    --allowed-o-auth-flows-user-pool-client \
    --supported-identity-providers "COGNITO" \
    --output json > /dev/null

  echo "[OK] Cognito callback URLs updated successfully."
fi

echo ""

# ============================================================================
# Stage 3: Update iac.ts with actual CloudFront domain
# ============================================================================
echo "=== Stage 3: Update iac.ts Hardcoded Values ==="

CURRENT_CF_DOMAIN=$(grep "cloudFrontDomainName.*=.*cloudfront.net" "$IAC_TS" | sed -E "s/.*'([a-z0-9]+\.cloudfront\.net)'.*/\1/")

if [ "$CURRENT_CF_DOMAIN" != "$ACTUAL_CF_DOMAIN" ]; then
  echo "[INFO] CloudFront domain in iac.ts is outdated."
  echo "  Current:  $CURRENT_CF_DOMAIN"
  echo "  Actual:   $ACTUAL_CF_DOMAIN"

  if [ "$DRY_RUN" = true ]; then
    echo "[DRY-RUN] Would update iac.ts CloudFront domain to: $ACTUAL_CF_DOMAIN"
  else
    # Use sed to replace the CloudFront domain in iac.ts
    if [[ "$OSTYPE" == "darwin"* ]]; then
      sed -i '' "s|$CURRENT_CF_DOMAIN|$ACTUAL_CF_DOMAIN|g" "$IAC_TS"
    else
      sed -i "s|$CURRENT_CF_DOMAIN|$ACTUAL_CF_DOMAIN|g" "$IAC_TS"
    fi
    echo "[OK] Updated iac.ts with actual CloudFront domain: $ACTUAL_CF_DOMAIN"
  fi
else
  echo "[OK] iac.ts CloudFront domain already matches: $ACTUAL_CF_DOMAIN"
fi

echo ""

# ============================================================================
# Stage 4: Re-validate
# ============================================================================
echo "=== Stage 4: Post-Deploy Validation ==="

if [ "$DRY_RUN" = true ]; then
  echo "[DRY-RUN] Would run: $SCRIPT_DIR/validate-config.sh"
else
  "$SCRIPT_DIR/validate-config.sh"
fi

echo ""
echo "================================================================"
echo " Two-Stage Deploy Complete"
echo ""
echo " CloudFront URL:  https://$ACTUAL_CF_DOMAIN"
echo " Cognito Domain:  $(aws cognito-idp describe-user-pool \
  --user-pool-id "$USER_POOL_ID" \
  --region "$COGNITO_REGION" \
  --query 'UserPool.Domain' \
  --output text 2>/dev/null || echo '(unknown)')"
echo "================================================================"
