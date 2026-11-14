#!/bin/bash
# Unit tests for scripts/lib/cloudfront-origin.sh — the pure decision
# functions scripts/deploy-all.sh uses for STRATOCLAVE_ENTRY /
# STRATOCLAVE_CLOUDFRONT_ORIGIN parsing and the vpc-origin migration guard.
#
# No `aws` CLI, no credentials, no network: every function under test takes
# its inputs as plain arguments (see that file's own header for why). Run:
#   iac/scripts/test-cloudfront-origin-guard.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
    echo "[FAIL] $desc"
    echo "        expected: $expected"
    echo "        actual:   $actual"
  fi
}

assert_true() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "[FAIL] $desc (expected success, got exit $?)"
  fi
}

assert_false() {
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    FAIL=$((FAIL + 1))
    echo "[FAIL] $desc (expected failure, got exit 0)"
  else
    PASS=$((PASS + 1))
  fi
}

echo "=== cfo_normalize ==="
assert_eq "trims and lowercases" "cloudfront" "$(cfo_normalize '  CloudFront  ')"
assert_eq "empty stays empty"    ""           "$(cfo_normalize '')"
assert_eq "whitespace-only becomes empty" "" "$(cfo_normalize '   ')"
# Round-1 implementation review (Sol): an earlier version used
# `tr -d '[:space:]'`, which deletes INTERNAL whitespace too, so
# "vpc -expand" normalized to "vpc-expand" -- a value cfo_validate_origin
# would then wrongly accept even though TypeScript's `.trim()` leaves the
# internal space in place and refuses it. The fix only trims the edges;
# this pins that an internal space survives normalization (and therefore
# still fails cfo_validate_origin's exact-string match below).
assert_eq "internal whitespace survives trim (parity with TS .trim())" \
  "vpc -expand" "$(cfo_normalize '  VPC -Expand  ')"
assert_false "normalized-but-internally-spaced value is still invalid" \
  cfo_validate_origin "$(cfo_normalize '  VPC -Expand  ')"

echo "=== cfo_validate_entry ==="
assert_true  "cloudfront is valid" cfo_validate_entry "cloudfront"
assert_true  "private is valid"    cfo_validate_entry "private"
assert_true  "both is valid"       cfo_validate_entry "both"
assert_false "typo is invalid"     cfo_validate_entry "cloudfrotn"
assert_false "empty is invalid"    cfo_validate_entry ""

echo "=== cfo_default_origin (the mode-aware silent default) ==="
# This is the regression this file exists to pin: defaulting
# STRATOCLAVE_CLOUDFRONT_ORIGIN to "vpc" unconditionally broke every
# pre-existing STRATOCLAVE_ENTRY=private deployment, which never set that
# variable at all (it did not exist until this feature did).
assert_eq "private's silent default is public, not vpc" "public" "$(cfo_default_origin 'private')"
assert_eq "both's silent default is vpc"                "vpc"    "$(cfo_default_origin 'both')"
assert_eq "cloudfront's silent default is vpc"          "vpc"    "$(cfo_default_origin 'cloudfront')"

echo "=== cfo_validate_origin ==="
assert_true  "public is valid"       cfo_validate_origin "public"
assert_true  "vpc-expand is valid"   cfo_validate_origin "vpc-expand"
assert_true  "vpc-switch is valid"   cfo_validate_origin "vpc-switch"
assert_true  "vpc-contract is valid" cfo_validate_origin "vpc-contract"
assert_true  "vpc is valid"          cfo_validate_origin "vpc"
assert_false "typo is invalid"       cfo_validate_origin "vpc-expnad"
assert_false "'both' value name, not a real origin value" cfo_validate_origin "both"

echo "=== cfo_check_combo ==="
assert_true  "private + public is allowed"      cfo_check_combo "private" "public"
assert_false "private + vpc is refused"         cfo_check_combo "private" "vpc"
assert_false "private + vpc-expand is refused"  cfo_check_combo "private" "vpc-expand"
assert_true  "both + vpc is allowed"            cfo_check_combo "both" "vpc"
assert_false "both + public is refused"         cfo_check_combo "both" "public"
assert_false "both + vpc-expand is refused"     cfo_check_combo "both" "vpc-expand"
assert_true  "cloudfront + public is allowed"   cfo_check_combo "cloudfront" "public"
assert_true  "cloudfront + vpc is allowed"      cfo_check_combo "cloudfront" "vpc"
assert_true  "cloudfront + vpc-expand is allowed" cfo_check_combo "cloudfront" "vpc-expand"

# Mutation check: deleting either refusal branch must fail a test. Assert
# the EXACT message content a deleted branch would stop producing, not just
# the exit code (an exit-code-only test would still pass if the message
# text were accidentally deleted but the `return 1` survived some other way).
echo "=== cfo_check_combo messages ==="
MSG="$(cfo_check_combo "private" "vpc")"
case "$MSG" in
  *"STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc has no effect under STRATOCLAVE_ENTRY=private"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] private+vpc message content: got '$MSG'" ;;
esac
MSG="$(cfo_check_combo "both" "public")"
case "$MSG" in
  *"STRATOCLAVE_CLOUDFRONT_ORIGIN=public is refused under STRATOCLAVE_ENTRY=both"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] both+public message content: got '$MSG'" ;;
esac

echo "=== cfo_migration_decision: nothing live -> always allowed ==="
assert_true "no ALB at all + vpc (the default)" cfo_migration_decision "vpc" "" "false" "false" "false"
assert_true "no ALB at all + vpc-switch"         cfo_migration_decision "vpc-switch" "" "false" "false" "false"
assert_true "no ALB at all + vpc-contract"       cfo_migration_decision "vpc-contract" "" "false" "false" "false"
assert_true "ALB exists but internal scheme (not this guard's concern) + vpc" \
  cfo_migration_decision "vpc" "internal" "false" "false" "false"

echo "=== cfo_migration_decision: live public ALB, origin=public|vpc-expand -> always allowed ==="
assert_true "live public + explicit public"    cfo_migration_decision "public" "internet-facing" "false" "false" "false"
assert_true "live public + vpc-expand, no internal yet" cfo_migration_decision "vpc-expand" "internet-facing" "false" "false" "false"
assert_true "live public + vpc-expand, internal already exists" cfo_migration_decision "vpc-expand" "internet-facing" "true" "false" "true"

echo "=== cfo_migration_decision: vpc-switch requires the internal ALB to exist (unchanged step) ==="
assert_false "live public ALB + vpc-switch + internal NOT yet built -> REFUSED" \
  cfo_migration_decision "vpc-switch" "internet-facing" "false" "false" "true"
assert_true "live public ALB + vpc-switch + internal ALREADY built -> ALLOWED" \
  cfo_migration_decision "vpc-switch" "internet-facing" "true" "false" "true"

echo "=== cfo_migration_decision: vpc-contract requires the switch to have ACTUALLY deployed (round-1 implementation review should-fix 2 / Sol's matching should-fix) ==="
assert_false "internal ALB exists but distribution not yet switched -> REFUSED" \
  cfo_migration_decision "vpc-contract" "internet-facing" "true" "false" "true"
assert_true "internal ALB exists AND distribution confirmed switched+Deployed -> ALLOWED" \
  cfo_migration_decision "vpc-contract" "internet-facing" "true" "true" "true"
# The regression this file exists to pin: presence of the internal ALB
# ALONE used to be enough to go straight from vpc-expand to vpc, skipping
# the independently-verified switch -- this case demonstrates that is no
# longer sufficient on its own for vpc-contract either.
assert_false "internal ALB present (vpc-expand only) but switch never verified -> REFUSED, not waved through on internal-ALB-presence alone" \
  cfo_migration_decision "vpc-contract" "internet-facing" "true" "false" "true"

echo "=== cfo_migration_decision: the vpc (contraction) refusal matrix (the security property under test) ==="
assert_false "live public ALB + default vpc + public origin STILL declared (vpc-contract never deployed) -> REFUSED" \
  cfo_migration_decision "vpc" "internet-facing" "true" "true" "true"
assert_true "live public ALB + vpc + public origin NO LONGER declared (vpc-contract already deployed) -> ALLOWED" \
  cfo_migration_decision "vpc" "internet-facing" "true" "true" "false"

echo "=== cfo_migration_decision messages name the fix ==="
MSG="$(cfo_migration_decision "vpc" "internet-facing" "true" "true" "true")"
case "$MSG" in
  *"vpc-expand"*"vpc-switch"*"vpc-contract"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] vpc refusal message should name all three prior steps: got '$MSG'" ;;
esac
MSG="$(cfo_migration_decision "vpc-contract" "internet-facing" "true" "false" "true")"
case "$MSG" in
  *"vpc-switch"*"Deployed"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] vpc-contract refusal message should name the vpc-switch/Deployed precondition: got '$MSG'" ;;
esac

echo "=== cfo_az_unsupported ==="
assert_true  "use1-az3 is unsupported"  cfo_az_unsupported "use1-az3"
assert_true  "apne1-az3 is unsupported" cfo_az_unsupported "apne1-az3"
assert_true  "usw1-az2 is unsupported"  cfo_az_unsupported "usw1-az2"
assert_true  "cac1-az3 is unsupported"  cfo_az_unsupported "cac1-az3"
assert_false "use1-az1 is supported"    cfo_az_unsupported "use1-az1"
assert_false "apne1-az1 is supported"   cfo_az_unsupported "apne1-az1"

echo ""
echo "=== deploy-all.sh itself: syntax + required functions present ==="
if bash -n "$SCRIPT_DIR/deploy-all.sh"; then
  PASS=$((PASS + 1))
else
  FAIL=$((FAIL + 1)); echo "[FAIL] deploy-all.sh has a syntax error"
fi
for fn in check_alb_scheme_guard check_cloudfront_origin_migration_guard check_vpc_origin_az_support; do
  if grep -q "^${fn}()" "$SCRIPT_DIR/deploy-all.sh"; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1)); echo "[FAIL] deploy-all.sh no longer defines $fn — a guard was removed"
  fi
done

echo ""
echo "$PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
