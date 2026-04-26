#!/bin/bash

# Configuration Validation Script (Triple-Check)
#
# Validates configuration consistency across three sources:
#   1. iac/bin/iac.ts         -- hardcoded fallback values
#   2. outputs.json           -- last CDK deploy output (local artifact)
#   3. CloudFormation Stacks  -- live AWS state
#
# Checks performed:
#   - Cognito domain prefix: iac.ts vs outputs.json vs CloudFormation
#   - CloudFront domain:     iac.ts vs outputs.json vs CloudFormation
#   - User Pool ID:          outputs.json vs CloudFormation
#
# Usage:
#   ./scripts/validate-config.sh            # Full validation (requires AWS credentials)
#   ./scripts/validate-config.sh --skip-aws # Offline mode (iac.ts vs outputs.json only)
#
# Exit codes:
#   0 - All validations passed (warnings are OK)
#   1 - Validation failed (errors found)

set -e

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
SKIP_AWS=false
for arg in "$@"; do
  case "$arg" in
    --skip-aws) SKIP_AWS=true ;;
    --help|-h)
      echo "Usage: $0 [--skip-aws]"
      echo "  --skip-aws  Skip AWS API calls (validate iac.ts vs outputs.json only)"
      exit 0
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IAC_TS="$IAC_DIR/bin/iac.ts"
OUTPUTS_JSON="$IAC_DIR/outputs.json"

ERRORS=0
WARNINGS=0

error() {
  echo "[ERROR] $1"
  ERRORS=$((ERRORS + 1))
}

warn() {
  echo "[WARNING] $1"
  WARNINGS=$((WARNINGS + 1))
}

info() {
  echo "[INFO] $1"
}

ok() {
  echo "[OK] $1"
}

# ============================================================================
# Helper: Extract value from outputs.json using basic tools (no jq dependency)
# ============================================================================
extract_outputs_json_value() {
  local key="$1"
  if [ ! -f "$OUTPUTS_JSON" ]; then
    echo ""
    return
  fi
  # Use python3 if available, fall back to grep/sed
  if command -v python3 &>/dev/null; then
    python3 -c "
import json, sys
with open('$OUTPUTS_JSON') as f:
    data = json.load(f)
for stack in data.values():
    if isinstance(stack, dict) and '$key' in stack:
        print(stack['$key'])
        sys.exit(0)
print('')
" 2>/dev/null
  else
    # Fallback: simple grep
    grep -o "\"$key\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" "$OUTPUTS_JSON" 2>/dev/null | head -1 | sed -E 's/.*"([^"]+)"$/\1/'
  fi
}

echo "================================================================"
echo " Stratoclave Configuration Validation"
echo "================================================================"
echo ""
info "Starting configuration validation..."
info "iac.ts path: $IAC_TS"
echo ""

# ============================================================================
# Section 0: AWS Account ID Validation
# ============================================================================
echo "--- AWS Account ID Validation ---"

if [ ! -f "$OUTPUTS_JSON" ]; then
  warn "outputs.json not found, skipping account validation"
else
  # outputs.json からアカウント ID を抽出
  if command -v python3 &>/dev/null; then
    OUTPUTS_ACCOUNT_ID=$(python3 -c "
import json, re, sys
with open('$OUTPUTS_JSON') as f:
    data = json.load(f)
for stack in data.values():
    if isinstance(stack, dict):
        for v in stack.values():
            m = re.search(r'arn:aws:[^:]+:[^:]*:([0-9]{12}):', str(v))
            if m:
                print(m.group(1))
                sys.exit(0)
print('')
" 2>/dev/null)
  else
    OUTPUTS_ACCOUNT_ID=""
  fi

  if [ -n "$OUTPUTS_ACCOUNT_ID" ]; then
    if [ "$SKIP_AWS" = true ]; then
      info "Skipping AWS account ID comparison (--skip-aws)"
      info "outputs.json account: $OUTPUTS_ACCOUNT_ID"
    else
      # 現在の AWS_ACCOUNT_ID を取得
      CURRENT_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "")

      if [ -n "$CURRENT_ACCOUNT_ID" ]; then
        if [ "$OUTPUTS_ACCOUNT_ID" != "$CURRENT_ACCOUNT_ID" ]; then
          echo ""
          error "outputs.json is from a different AWS account"
          echo "  outputs.json account: $OUTPUTS_ACCOUNT_ID"
          echo "  Current account:      $CURRENT_ACCOUNT_ID"
          echo ""
          echo "  Fix: Re-generate outputs.json with 'cdk deploy --outputs-file outputs.json'"
          echo ""
        else
          ok "AWS Account ID matches: $CURRENT_ACCOUNT_ID"
        fi
      else
        warn "Could not retrieve current AWS account ID (check credentials)"
      fi
    fi
  else
    warn "Could not extract account ID from outputs.json"
  fi
fi

echo ""

# ============================================================================
# 1. Cognito Domain Validation
# ============================================================================
echo "--- Cognito Domain Validation ---"

# 1a. Extract hardcoded domain from iac.ts
HARDCODED_DOMAIN=$(grep "cognitoDomainPrefix.*=.*'stratoclave-" "$IAC_TS" | sed -E "s/.*'(stratoclave-[0-9]+)'.*/\1/")

if [ -z "$HARDCODED_DOMAIN" ]; then
  error "Failed to extract hardcoded Cognito domain from $IAC_TS"
else
  info "Hardcoded domain in iac.ts: $HARDCODED_DOMAIN"
fi

# 1b. Get actual Cognito domain from CloudFormation
ACTUAL_USER_POOL_ID=""
ACTUAL_COGNITO_DOMAIN=""

if [ "$SKIP_AWS" = true ]; then
  info "Skipping CloudFormation Cognito check (--skip-aws)"
else
  ACTUAL_USER_POOL_ID=$(aws cloudformation describe-stacks \
    --stack-name StratoclaveCognitoStack \
    --region us-east-1 \
    --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
    --output text 2>/dev/null || echo "")

  if [ -z "$ACTUAL_USER_POOL_ID" ] || [ "$ACTUAL_USER_POOL_ID" = "None" ]; then
    warn "StratoclaveCognitoStack not found in CloudFormation."
    warn "This is expected for first-time deployments. Skipping CloudFormation comparison."
    ACTUAL_USER_POOL_ID=""
  else
    info "User Pool ID (CloudFormation): $ACTUAL_USER_POOL_ID"

    ACTUAL_COGNITO_DOMAIN=$(aws cognito-idp describe-user-pool \
      --user-pool-id "$ACTUAL_USER_POOL_ID" \
      --region us-east-1 \
      --query 'UserPool.Domain' \
      --output text 2>/dev/null || echo "")

    if [ -z "$ACTUAL_COGNITO_DOMAIN" ] || [ "$ACTUAL_COGNITO_DOMAIN" = "None" ]; then
      error "Failed to retrieve Cognito domain for User Pool $ACTUAL_USER_POOL_ID"
      ACTUAL_COGNITO_DOMAIN=""
    else
      info "Actual Cognito domain (CloudFormation): $ACTUAL_COGNITO_DOMAIN"

      # Compare hardcoded vs actual
      if [ -n "$HARDCODED_DOMAIN" ] && [ "$ACTUAL_COGNITO_DOMAIN" != "$HARDCODED_DOMAIN" ]; then
        echo ""
        error "Cognito domain mismatch detected!"
        echo "  CloudFormation actual: $ACTUAL_COGNITO_DOMAIN"
        echo "  iac.ts hardcoded:      $HARDCODED_DOMAIN"
        echo ""
        echo "  This will cause authentication errors like:"
        echo "    https://${HARDCODED_DOMAIN}.auth.us-east-1.amazoncognito.com/error"
        echo "    'An error was encountered with the requested page.'"
        echo ""
        echo "  Fix: Update $IAC_TS line 36 to:"
        echo "    const cognitoDomainPrefix = process.env.COGNITO_DOMAIN_PREFIX || '$ACTUAL_COGNITO_DOMAIN';"
        echo ""
      elif [ -n "$HARDCODED_DOMAIN" ]; then
        ok "Cognito domain: iac.ts matches CloudFormation"
      fi
    fi
  fi
fi

# 1c. Validate outputs.json Cognito domain
if [ -f "$OUTPUTS_JSON" ]; then
  OUTPUTS_COGNITO_DOMAIN_URL=$(extract_outputs_json_value "CognitoDomain")
  OUTPUTS_USER_POOL_ID=$(extract_outputs_json_value "UserPoolId")

  if [ -n "$OUTPUTS_COGNITO_DOMAIN_URL" ]; then
    # Extract prefix from full URL: https://stratoclave-XXXX.auth.us-east-1.amazoncognito.com -> stratoclave-XXXX
    OUTPUTS_COGNITO_PREFIX=$(echo "$OUTPUTS_COGNITO_DOMAIN_URL" | sed -E 's|https://([^.]+)\.auth\..*|\1|')
    info "Cognito domain in outputs.json: $OUTPUTS_COGNITO_PREFIX (from $OUTPUTS_COGNITO_DOMAIN_URL)"

    # Compare outputs.json vs iac.ts
    if [ -n "$HARDCODED_DOMAIN" ] && [ "$OUTPUTS_COGNITO_PREFIX" != "$HARDCODED_DOMAIN" ]; then
      echo ""
      error "outputs.json Cognito domain is STALE!"
      echo "  outputs.json: $OUTPUTS_COGNITO_PREFIX"
      echo "  iac.ts:       $HARDCODED_DOMAIN"
      echo ""
      echo "  outputs.json contains an outdated Cognito domain prefix."
      echo "  Any process referencing outputs.json will use the wrong domain."
      echo ""
      echo "  Fix: Re-export outputs after deployment:"
      echo "    cd $IAC_DIR"
      echo "    cdk deploy --outputs-file outputs.json"
      echo ""
    else
      ok "Cognito domain: outputs.json matches iac.ts"
    fi

    # Compare outputs.json vs CloudFormation
    if [ -n "$ACTUAL_COGNITO_DOMAIN" ] && [ "$OUTPUTS_COGNITO_PREFIX" != "$ACTUAL_COGNITO_DOMAIN" ]; then
      echo ""
      error "outputs.json Cognito domain does not match CloudFormation!"
      echo "  outputs.json:     $OUTPUTS_COGNITO_PREFIX"
      echo "  CloudFormation:   $ACTUAL_COGNITO_DOMAIN"
      echo ""
      echo "  Fix: Re-export outputs after deployment:"
      echo "    cd $IAC_DIR"
      echo "    cdk deploy --outputs-file outputs.json"
      echo ""
    elif [ -n "$ACTUAL_COGNITO_DOMAIN" ]; then
      ok "Cognito domain: outputs.json matches CloudFormation"
    fi
  else
    warn "Could not extract CognitoDomain from outputs.json"
  fi

  # Validate User Pool ID consistency
  if [ -n "$OUTPUTS_USER_POOL_ID" ] && [ -n "$ACTUAL_USER_POOL_ID" ]; then
    if [ "$OUTPUTS_USER_POOL_ID" != "$ACTUAL_USER_POOL_ID" ]; then
      echo ""
      error "User Pool ID mismatch between outputs.json and CloudFormation!"
      echo "  outputs.json:   $OUTPUTS_USER_POOL_ID"
      echo "  CloudFormation: $ACTUAL_USER_POOL_ID"
      echo ""
      echo "  This indicates outputs.json is severely outdated."
      echo "  All references to User Pool ID from outputs.json are INVALID."
      echo ""
      echo "  Fix: Re-export outputs after deployment:"
      echo "    cd $IAC_DIR"
      echo "    cdk deploy --outputs-file outputs.json"
      echo ""
    else
      ok "User Pool ID: outputs.json matches CloudFormation"
    fi
  fi
else
  info "outputs.json not found at $OUTPUTS_JSON (this is normal before first deployment)"
fi

echo ""

# ============================================================================
# 2. CloudFront Domain Validation
# ============================================================================
echo "--- CloudFront Domain Validation ---"

# 2a. Extract hardcoded CloudFront domain from iac.ts
HARDCODED_CF_DOMAIN=$(grep "cloudFrontDomainName.*=.*cloudfront.net" "$IAC_TS" | sed -E "s/.*'([a-z0-9]+\.cloudfront\.net)'.*/\1/")

if [ -z "$HARDCODED_CF_DOMAIN" ]; then
  error "Failed to extract hardcoded CloudFront domain from $IAC_TS"
else
  info "Hardcoded CloudFront domain in iac.ts: $HARDCODED_CF_DOMAIN"
fi

# 2b. Get actual CloudFront domain from CloudFormation
ACTUAL_CLOUDFRONT_DOMAIN=""

if [ "$SKIP_AWS" = true ]; then
  info "Skipping CloudFormation CloudFront check (--skip-aws)"
else
  ACTUAL_CLOUDFRONT_DOMAIN=$(aws cloudformation describe-stacks \
    --stack-name StratoclaveFrontendStack \
    --query 'Stacks[0].Outputs[?OutputKey==`CloudFrontDomainName`].OutputValue' \
    --output text 2>/dev/null || echo "")

  if [ -z "$ACTUAL_CLOUDFRONT_DOMAIN" ] || [ "$ACTUAL_CLOUDFRONT_DOMAIN" = "None" ]; then
    warn "StratoclaveFrontendStack not found. Skipping CloudFront CloudFormation comparison."
    warn "For initial deployment, use 'scripts/deploy-with-update.sh' to handle the chicken-egg problem."
    ACTUAL_CLOUDFRONT_DOMAIN=""
  else
    info "Actual CloudFront domain (CloudFormation): $ACTUAL_CLOUDFRONT_DOMAIN"

    if [ -n "$HARDCODED_CF_DOMAIN" ] && [ "$ACTUAL_CLOUDFRONT_DOMAIN" != "$HARDCODED_CF_DOMAIN" ]; then
      echo ""
      error "CloudFront domain mismatch!"
      echo "  CloudFormation actual: $ACTUAL_CLOUDFRONT_DOMAIN"
      echo "  iac.ts hardcoded:      $HARDCODED_CF_DOMAIN"
      echo ""
      echo "  Fix: Update $IAC_TS line 32 to:"
      echo "    const cloudFrontDomainName = process.env.CLOUDFRONT_DOMAIN || '$ACTUAL_CLOUDFRONT_DOMAIN';"
      echo ""
    elif [ -n "$HARDCODED_CF_DOMAIN" ]; then
      ok "CloudFront domain: iac.ts matches CloudFormation"
    fi
  fi
fi

# 2c. Validate outputs.json CloudFront domain
if [ -f "$OUTPUTS_JSON" ]; then
  OUTPUTS_CF_DOMAIN=$(extract_outputs_json_value "CloudFrontDomainName")

  if [ -n "$OUTPUTS_CF_DOMAIN" ]; then
    info "CloudFront domain in outputs.json: $OUTPUTS_CF_DOMAIN"

    # Compare outputs.json vs iac.ts
    if [ -n "$HARDCODED_CF_DOMAIN" ] && [ "$OUTPUTS_CF_DOMAIN" != "$HARDCODED_CF_DOMAIN" ]; then
      echo ""
      error "outputs.json CloudFront domain does not match iac.ts!"
      echo "  outputs.json: $OUTPUTS_CF_DOMAIN"
      echo "  iac.ts:       $HARDCODED_CF_DOMAIN"
      echo ""
      echo "  Fix: Update iac.ts or re-export outputs:"
      echo "    const cloudFrontDomainName = process.env.CLOUDFRONT_DOMAIN || '$OUTPUTS_CF_DOMAIN';"
      echo ""
    else
      ok "CloudFront domain: outputs.json matches iac.ts"
    fi

    # Compare outputs.json vs CloudFormation
    if [ -n "$ACTUAL_CLOUDFRONT_DOMAIN" ] && [ "$OUTPUTS_CF_DOMAIN" != "$ACTUAL_CLOUDFRONT_DOMAIN" ]; then
      echo ""
      error "outputs.json CloudFront domain does not match CloudFormation!"
      echo "  outputs.json:   $OUTPUTS_CF_DOMAIN"
      echo "  CloudFormation: $ACTUAL_CLOUDFRONT_DOMAIN"
      echo ""
      echo "  Fix: Re-export outputs:"
      echo "    cd $IAC_DIR"
      echo "    cdk deploy --outputs-file outputs.json"
      echo ""
    elif [ -n "$ACTUAL_CLOUDFRONT_DOMAIN" ]; then
      ok "CloudFront domain: outputs.json matches CloudFormation"
    fi
  else
    warn "CloudFrontDomainName not found in outputs.json (StratoclaveFrontendStack section missing)"
    warn "This is expected on first deploy (chicken-egg problem)."
    warn "Run 'scripts/deploy-with-update.sh' for initial deployment."
  fi
fi

echo ""

# ============================================================================
# 3. Summary
# ============================================================================
echo "================================================================"
if [ $ERRORS -gt 0 ]; then
  echo " VALIDATION FAILED: $ERRORS error(s), $WARNINGS warning(s)"
  echo ""
  echo " Authentication errors will occur if these mismatches are not fixed."
  echo " See error details above for specific fix instructions."
  echo "================================================================"
  exit 1
elif [ $WARNINGS -gt 0 ]; then
  echo " VALIDATION PASSED with $WARNINGS warning(s)"
  echo ""
  echo " Warnings are expected for first-time deployments or when"
  echo " some stacks have not been deployed yet."
  echo "================================================================"
  exit 0
else
  echo " VALIDATION PASSED: All checks succeeded"
  echo "================================================================"
  exit 0
fi
