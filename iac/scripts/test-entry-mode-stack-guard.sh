#!/bin/bash
# Unit tests for `check_entry_mode_stack_guard`, deploy-all.sh's entry-mode
# switch detector that does not depend on any ALB's name (round-1
# implementation review, item 11): it compares whether FrontendStack and
# PrivateLinkStack exist against this run's STRATOCLAVE_ENTRY, which
# catches the `vpc <-> private` and `private <-> both` switches
# `check_alb_scheme_guard` cannot see (STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc
# never names an ALB "${PREFIX}-alb" at all).
#
# A stubbed `aws` on PATH drives `stack_exists`, same pattern as
# test-aws-lookup-or-empty.sh. Both functions are extracted from
# deploy-all.sh with `sed`, not hand-duplicated, so this test exercises the
# actual source rather than a copy that could drift.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ALL="$SCRIPT_DIR/deploy-all.sh"

log_warn()  { echo "[WARN]  $1" >&2; }
log_error() { echo "[ERROR] $1" >&2; }

EXTRACTED="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -f "$EXTRACTED"; rm -rf "$STUB_DIR"' EXIT

{
  sed -n '/^stack_exists() {/,/^}/p' "$DEPLOY_ALL"
  echo
  sed -n '/^check_entry_mode_stack_guard() {/,/^}/p' "$DEPLOY_ALL"
} > "$EXTRACTED"

if ! grep -q '^stack_exists() {' "$EXTRACTED" || ! grep -q '^check_entry_mode_stack_guard() {' "$EXTRACTED"; then
  echo "[FAIL] could not extract stack_exists()/check_entry_mode_stack_guard() from $DEPLOY_ALL — did either definition move or its signature change?"
  exit 1
fi
# shellcheck disable=SC1090
source "$EXTRACTED"

PASS=0
FAIL=0

assert_eq() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $desc"
    echo "        expected: $expected"
    echo "        actual:   $actual"
  fi
}

PATH="$STUB_DIR:$PATH"
REGION="us-east-1"
FRONTEND_STACK="stratoclave-frontend"
PRIVATE_LINK_STACK="stratoclave-private-link"
DRY_RUN=false

# A stub `aws cloudformation describe-stacks` that reports a fixed set of
# "live" stack names as CREATE_COMPLETE and everything else as not found
# (ValidationError, exit 254 — the real AWS CLI's shape for an unknown
# stack name).
stub_live_stacks() {
  # $@ = the stack names that exist
  local live="$*"
  cat > "$STUB_DIR/aws" <<EOF
#!/bin/bash
if [ "\$1" = "cloudformation" ] && [ "\$2" = "describe-stacks" ]; then
  name=""
  prev=""
  for arg in "\$@"; do
    if [ "\$prev" = "--stack-name" ]; then name="\$arg"; fi
    prev="\$arg"
  done
  for live_name in $live; do
    if [ "\$name" = "\$live_name" ]; then
      echo "CREATE_COMPLETE"
      exit 0
    fi
  done
  echo "An error occurred (ValidationError) when calling the DescribeStacks operation: Stack with id \$name does not exist" >&2
  exit 254
fi
echo "unexpected aws invocation: \$*" >&2
exit 1
EOF
  chmod +x "$STUB_DIR/aws"
}

echo "=== check_entry_mode_stack_guard: fresh prefix (neither stack exists) -> always allowed ==="
stub_live_stacks
for ENTRY in cloudfront private both; do
  ( ENTRY="$ENTRY" check_entry_mode_stack_guard )
  RC=$?
  assert_eq "fresh prefix, ENTRY=$ENTRY: allowed" "0" "$RC"
done

echo "=== check_entry_mode_stack_guard: matching entry mode -> allowed ==="
stub_live_stacks "$FRONTEND_STACK"
( ENTRY="cloudfront" check_entry_mode_stack_guard )
assert_eq "cloudfront deployed, cloudfront requested: allowed" "0" "$?"

stub_live_stacks "$PRIVATE_LINK_STACK"
( ENTRY="private" check_entry_mode_stack_guard )
assert_eq "private deployed, private requested: allowed" "0" "$?"

stub_live_stacks "$FRONTEND_STACK" "$PRIVATE_LINK_STACK"
( ENTRY="both" check_entry_mode_stack_guard )
assert_eq "both deployed, both requested: allowed" "0" "$?"

echo "=== check_entry_mode_stack_guard: THE REGRESSION THIS FILE EXISTS TO PIN -- vpc <-> private and private <-> both, which check_alb_scheme_guard alone cannot see ==="
# STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc never names an ALB "${PREFIX}-alb", so
# the deployed state here (cloudfront entry, FrontendStack exists, no
# PrivateLinkStack) looks IDENTICAL to a cloudfront entry under
# cloudfrontOrigin=public to this guard -- which is correct, because this
# guard's whole point is to catch the ENTRY switch regardless of
# cloudfrontOrigin, using a signal check_alb_scheme_guard cannot use at all.
stub_live_stacks "$FRONTEND_STACK"
( ENTRY="private" check_entry_mode_stack_guard > /tmp/out.$$ 2>&1 )
RC=$?
OUT="$(cat /tmp/out.$$ 2>/dev/null || true)"; rm -f /tmp/out.$$
assert_eq "cloudfront/vpc deployed, private requested: REFUSED" "1" "$RC"
case "$OUT" in
  *"STRATOCLAVE_ENTRY=private"*"deploy used STRATOCLAVE_ENTRY=cloudfront"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] refusal message should name both entry values: got '$OUT'" ;;
esac

stub_live_stacks "$PRIVATE_LINK_STACK"
( ENTRY="both" check_entry_mode_stack_guard > /tmp/out.$$ 2>&1 )
RC=$?
OUT="$(cat /tmp/out.$$ 2>/dev/null || true)"; rm -f /tmp/out.$$
assert_eq "private deployed, both requested: REFUSED" "1" "$RC"
case "$OUT" in
  *"STRATOCLAVE_ENTRY=both"*"deploy used STRATOCLAVE_ENTRY=private"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] refusal message should name both entry values: got '$OUT'" ;;
esac

echo "=== check_entry_mode_stack_guard: --dry-run warns but does not exit ==="
stub_live_stacks "$FRONTEND_STACK"
( DRY_RUN=true ENTRY="private" check_entry_mode_stack_guard > /dev/null 2>&1 )
assert_eq "dry-run: does not abort" "0" "$?"

echo ""
echo "$PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
