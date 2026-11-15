#!/bin/bash
# Unit tests for `check_cloudfront_origin_migration_guard`, the AWS-querying
# wrapper in deploy-all.sh that resolves `cfo_migration_decision`'s inputs
# (scripts/lib/cloudfront-origin.sh) from the live deployment. Companion to
# test-cloudfront-origin-guard.sh (which only exercises the PURE decision
# function with hand-picked inputs) and test-aws-lookup-or-empty.sh (which
# established the stub-`aws`-on-PATH / sed-extraction pattern this file
# reuses) — neither exercised THIS wrapper's own AWS-querying logic at all.
#
# The case that matters most: a live, never-migrated, internet-facing
# deployment (no internal ALB yet, the distribution still declaring the public
# ALBOrigin) with STRATOCLAVE_CLOUDFRONT_ORIGIN unset (default "vpc") must be
# refused before any stack is touched. The decision function alone cannot see
# this; the wrapper has to read the live distribution even when no internal
# ALB exists.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ALL="$SCRIPT_DIR/deploy-all.sh"

log_error() { echo "[ERROR] $1" >&2; }
log_warn()  { echo "[WARN] $1"; }

EXTRACTED="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -f "$EXTRACTED"; rm -rf "$STUB_DIR"' EXIT

for fn in aws_lookup_or_empty get_stack_output stack_exists check_cloudfront_origin_migration_guard; do
  sed -n "/^${fn}() {/,/^}/p" "$DEPLOY_ALL" >> "$EXTRACTED"
  echo >> "$EXTRACTED"
done
if [ ! -s "$EXTRACTED" ]; then
  echo "[FAIL] could not extract the guard wrapper and its helpers from $DEPLOY_ALL"
  exit 1
fi
# shellcheck disable=SC1090
source "$EXTRACTED"
# shellcheck source=lib/cloudfront-origin.sh
source "$SCRIPT_DIR/lib/cloudfront-origin.sh"

PASS=0
FAIL=0
assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $desc (expected '$expected', got '$actual')"
  fi
}

PATH="$STUB_DIR:$PATH"
PREFIX="example"
REGION="us-east-1"
ENTRY="cloudfront"
ALB_STACK="${PREFIX}-alb"
FRONTEND_STACK="${PREFIX}-frontend"

# $1 InternalAlbDnsName output ("" = vpc-expand never ran)
# $2 ALBOrigin count still in the distribution's origins ("0" or "1")
# $3 distribution Status ("Deployed" by default)
# $4 TargetOriginId of behaviour 0 ("ALBOrigin" by default, "ALBVpcOrigin" once vpc-switch ran)
write_stub_scenario() {
  local internal_dns="$1" origin_count="$2" dist_status="${3:-Deployed}" target_origin="${4:-ALBOrigin}"
  cat > "$STUB_DIR/aws" <<EOF
#!/bin/bash
a="\$*"
case "\$a" in
  *"elbv2 describe-load-balancers"*)
    echo "internet-facing"; exit 0 ;;
  *"cloudformation describe-stacks"*"StackStatus"*)
    echo "UPDATE_COMPLETE"; exit 0 ;;
  *"cloudformation describe-stacks"*"InternalAlbDnsName"*)
    echo "$internal_dns"; exit 0 ;;
  *"cloudformation describe-stacks"*"CloudFrontDistributionId"*)
    echo "DISTID123"; exit 0 ;;
  *"cloudfront get-distribution --id"*)
    echo "$dist_status"; exit 0 ;;
  *"cloudfront get-distribution-config --id"*"TargetOriginId"*)
    echo "$target_origin"; exit 0 ;;
  *"cloudfront get-distribution-config --id"*"ALBOrigin"*)
    echo "$origin_count"; exit 0 ;;
  *) echo "unstubbed aws call: \$a" >&2; exit 1 ;;
esac
EOF
  chmod +x "$STUB_DIR/aws"
}

run_guard() {
  # Runs in a subshell: the wrapper's own `exit 1` on refusal must not kill
  # this test script.
  ( CLOUDFRONT_ORIGIN="$1" DRY_RUN=false check_cloudfront_origin_migration_guard ) \
    > /tmp/guard_out.$$ 2>/tmp/guard_err.$$
  echo "$?"
}

echo "=== regression: never-migrated live deployment (no vpc-expand run yet), origin=vpc ==="
# live_internal_alb_present=false, distribution still declares ALBOrigin.
# A never-migrated, internet-facing deployment. Must refuse.
write_stub_scenario "" "1"
RC="$(run_guard vpc)"
assert_eq "origin=vpc refused before a never-migrated deployment's public ALB is touched" "1" "$RC"
case "$(cat /tmp/guard_err.$$)" in
  *"live internet-facing ALB exists"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] refusal message missing (got: $(cat /tmp/guard_err.$$))" ;;
esac

echo "=== origin=vpc-expand on the same never-migrated deployment: always allowed ==="
RC="$(run_guard vpc-expand)"
assert_eq "vpc-expand is allowed (the step that is supposed to run first)" "0" "$RC"

echo "=== origin=vpc after vpc-expand+vpc-switch but BEFORE vpc-contract: still refused ==="
# internal ALB exists, switch has deployed, but FrontendStack/EcsStack still
# consume the public ALB's exports (ALBOrigin still declared) -- vpc-contract
# has not run yet.
write_stub_scenario "internal-alb.example.com" "1" "Deployed" "ALBVpcOrigin"
RC="$(run_guard vpc)"
assert_eq "origin=vpc still refused before vpc-contract has deployed" "1" "$RC"

echo "=== origin=vpc after the full vpc-expand -> vpc-switch -> vpc-contract sequence: allowed ==="
# vpc-contract has deployed: FrontendStack no longer declares ALBOrigin.
write_stub_scenario "internal-alb.example.com" "0" "Deployed" "ALBVpcOrigin"
RC="$(run_guard vpc)"
assert_eq "origin=vpc allowed once vpc-contract has actually deployed" "0" "$RC"

rm -f /tmp/guard_out.$$ /tmp/guard_err.$$

echo ""
echo "$PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
