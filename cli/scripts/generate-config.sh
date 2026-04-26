#!/bin/bash
set -e

# Generate config.toml from CDK outputs.json
#
# Usage:
#   ./generate-config.sh                        # uses default path ../iac/outputs.json
#   ./generate-config.sh /path/to/outputs.json  # uses specified path
#   OUTPUTS_FILE=/path/to/outputs.json ./generate-config.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTPUTS_FILE="${OUTPUTS_FILE:-${1:-${SCRIPT_DIR}/../../iac/outputs.json}}"
CONFIG_OUTPUT="${CONFIG_OUTPUT:-${SCRIPT_DIR}/../config.toml}"

echo "[INFO] Reading CDK outputs from: $OUTPUTS_FILE"

if [ ! -f "$OUTPUTS_FILE" ]; then
  echo "[ERROR] outputs.json not found at $OUTPUTS_FILE"
  echo "[ERROR] Run 'cd iac && npx cdk deploy --outputs-file outputs.json' first."
  exit 1
fi

# Check jq is available
if ! command -v jq &>/dev/null; then
  echo "[ERROR] jq is required but not installed. Install with: brew install jq"
  exit 1
fi

# Extract values from outputs.json
CLIENT_ID=$(jq -r '.StratoclaveCognitoStack.UserPoolClientId // empty' "$OUTPUTS_FILE")
COGNITO_DOMAIN=$(jq -r '.StratoclaveCognitoStack.CognitoDomain // empty' "$OUTPUTS_FILE")
ALB_DNS=$(jq -r '.StratoclaveAlbStack.AlbDnsName // empty' "$OUTPUTS_FILE")

# Validate extracted values
MISSING=()
[ -z "$CLIENT_ID" ] && MISSING+=("StratoclaveCognitoStack.UserPoolClientId")
[ -z "$COGNITO_DOMAIN" ] && MISSING+=("StratoclaveCognitoStack.CognitoDomain")
[ -z "$ALB_DNS" ] && MISSING+=("StratoclaveAlbStack.AlbDnsName")

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "[ERROR] Missing required values in outputs.json:"
  for m in "${MISSING[@]}"; do
    echo "  - $m"
  done
  exit 1
fi

# Construct API endpoint from ALB DNS
API_ENDPOINT="http://${ALB_DNS}"

# Write config.toml
cat > "$CONFIG_OUTPUT" <<EOF
# Stratoclave CLI Configuration
# Auto-generated from CDK outputs.json
# Generated at: $(date -u +"%Y-%m-%dT%H:%M:%SZ")

[auth]
client_id = "$CLIENT_ID"
cognito_domain = "$COGNITO_DOMAIN"
redirect_uri = "http://localhost:18080/callback"

[api]
endpoint = "$API_ENDPOINT"
EOF

echo "[INFO] config.toml generated at: $CONFIG_OUTPUT"
echo ""
echo "  [auth]"
echo "  client_id = \"$CLIENT_ID\""
echo "  cognito_domain = \"$COGNITO_DOMAIN\""
echo "  redirect_uri = \"http://localhost:18080/callback\""
echo ""
echo "  [api]"
echo "  endpoint = \"$API_ENDPOINT\""
echo ""
echo "[INFO] Done. Review and adjust redirect_uri if needed."
