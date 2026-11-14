#!/bin/bash
# Unit tests for `aws_lookup_or_empty`, the deploy-all.sh helper that tells
# "this resource does not exist yet" (LoadBalancerNotFound, ParameterNotFound,
# ...) apart from a genuine AWS CLI failure under `set -euo pipefail`.
#
# Round-1 implementation review (Fable B2, Sol's two matching blockers): a
# plain `x=$(aws ... 2>/dev/null)` aborts deploy-all.sh on ANY non-zero `aws`
# exit code, including the routine "not found" case the new 'vpc' default
# and the 'vpc-contract' step both produce on every redeploy. The existing
# test-cloudfront-origin-guard.sh only exercises the PURE decision functions
# (no `aws` calls at all); this file is the one that puts a stubbed `aws`
# binary on PATH and drives the actual wrapper, per that review's explicit
# ask: "Test the wrapper paths with a stubbed `aws` on PATH (exit 254 +
# error text) not just the pure functions."
#
# `deploy-all.sh` itself is a monolithic top-level script (argument parsing,
# AWS calls, and real side effects all run at source time, not behind a
# `main()` guard), so it cannot be safely `source`d here just to reach one
# function. Instead, this file extracts ONLY `aws_lookup_or_empty`'s
# definition out of deploy-all.sh with `sed` (by its start/end markers,
# below) and sources THAT — so a change to the function's actual source is
# what this test exercises, not a hand-copied duplicate that could drift.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ALL="$SCRIPT_DIR/deploy-all.sh"

# log_error is the one dependency `aws_lookup_or_empty` has outside itself;
# stub it (writing to stderr, same as deploy-all.sh's own `log_error`) so a
# genuine-failure path can be observed without deploy-all.sh's real
# `tee -a "$DEPLOYMENT_LOG"` (which needs a real log file this test does
# not set up).
log_error() { echo "[ERROR] $1" >&2; }

EXTRACTED="$(mktemp)"
trap 'rm -f "$EXTRACTED"' EXIT
sed -n '/^aws_lookup_or_empty() {/,/^}/p' "$DEPLOY_ALL" > "$EXTRACTED"
if [ ! -s "$EXTRACTED" ]; then
  echo "[FAIL] could not extract aws_lookup_or_empty() from $DEPLOY_ALL — did its definition move or its signature change?"
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

# A stub `aws` on PATH, overriding any real one found earlier in PATH for
# the duration of each test below.
STUB_DIR="$(mktemp -d)"
trap 'rm -f "$EXTRACTED"; rm -rf "$STUB_DIR"' EXIT
PATH="$STUB_DIR:$PATH"

write_stub() {
  # $1 = exit code, $2 = stderr text
  cat > "$STUB_DIR/aws" <<EOF
#!/bin/bash
echo "$2" >&2
exit $1
EOF
  chmod +x "$STUB_DIR/aws"
}

echo "=== aws_lookup_or_empty: a real AWS CLI error (exit 254) with a recognised 'not found' signature returns empty, exit 0 ==="
write_stub 254 "An error occurred (LoadBalancerNotFound) when calling the DescribeLoadBalancers operation: Load balancers 'x' not found"
OUT="$(aws_lookup_or_empty "test lookup" aws elbv2 describe-load-balancers)"
RC=$?
assert_eq "LoadBalancerNotFound: empty stdout" "" "$OUT"
assert_eq "LoadBalancerNotFound: exit 0 (not a script abort)" "0" "$RC"

write_stub 254 "An error occurred (ParameterNotFound) when calling the GetParameter operation: ..."
OUT="$(aws_lookup_or_empty "test lookup" aws ssm get-parameter)"
RC=$?
assert_eq "ParameterNotFound: empty stdout" "" "$OUT"
assert_eq "ParameterNotFound: exit 0" "0" "$RC"

echo "=== aws_lookup_or_empty: a genuine failure (exit 254, unrecognised error) exits the caller loudly, NOT treated as 'not found' ==="
write_stub 254 "An error occurred (AccessDenied) when calling the DescribeLoadBalancers operation: not authorized"
( aws_lookup_or_empty "test lookup" aws elbv2 describe-load-balancers > /dev/null 2>/tmp/err.$$ )
RC=$?
ERR_MSG="$(cat /tmp/err.$$ 2>/dev/null || true)"
rm -f /tmp/err.$$
assert_eq "AccessDenied: subshell exits non-zero (the caller's own 'exit 1' fires)" "1" "$RC"
case "$ERR_MSG" in
  *"AccessDenied"*) PASS=$((PASS + 1)) ;;
  *) FAIL=$((FAIL + 1)); echo "[FAIL] genuine-failure path should log the real stderr, not swallow it: got '$ERR_MSG'" ;;
esac

echo "=== aws_lookup_or_empty: a successful call returns the real value, exit 0 ==="
cat > "$STUB_DIR/aws" <<'EOF'
#!/bin/bash
echo "internet-facing"
exit 0
EOF
chmod +x "$STUB_DIR/aws"
OUT="$(aws_lookup_or_empty "test lookup" aws elbv2 describe-load-balancers)"
RC=$?
assert_eq "success: real value passed through" "internet-facing" "$OUT"
assert_eq "success: exit 0" "0" "$RC"

echo ""
echo "$PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
