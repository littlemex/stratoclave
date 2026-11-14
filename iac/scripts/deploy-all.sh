#!/bin/bash
set -euo pipefail

###############################################################################
# Stratoclave Full Deployment Script
#
# Deploys all CDK stacks in dependency order, then — in the default
# `cloudfront` entry mode only — builds and deploys the Frontend assets to
# S3 / CloudFront. The Cognito callback URL is resolved via a cross-stack
# reference (`frontendStack.cfnDistribution.attrDomainName`), so no second
# Cognito deploy is necessary.
#
# Stack order, `STRATOCLAVE_ENTRY=cloudfront` (default; see iac/bin/iac.ts).
# `STRATOCLAVE_CLOUDFRONT_ORIGIN` (default: `vpc`) decides whether
# <Prefix>AlbStack builds a public ALB, an internal one, or both at once —
# see that variable's own entry below and docs/DEPLOYMENT.md's "VPC origin"
# section; it does not change this stack LIST, only what AlbStack and
# FrontendStack build inside it:
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack           (public ALB, internal ALB, or both — see STRATOCLAVE_CLOUDFRONT_ORIGIN)
#   5. <Prefix>WafStack           (CLOUDFRONT scope, us-east-1)
#   6. <Prefix>FrontendStack      (S3 + CloudFront)
#   7. <Prefix>CognitoStack       (receives CloudFront domain via cross-stack)
#   8. <Prefix>EcsStack
#   9. <Prefix>ConfigStack        (SSM parameters for runtime consumers)
#
# Then:
#  10. Build the Frontend with the CDK outputs embedded and sync to S3
#  11. Invalidate CloudFront
#
# Stack order, `STRATOCLAVE_ENTRY=private` (headless, PrivateLink only):
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack           (internal; no port 80)
#   5. <Prefix>PrivateLinkStack   (internal NLB + VPC endpoint service)
#   6. <Prefix>CognitoStack
#   7. <Prefix>EcsStack
#   8. <Prefix>ConfigStack
# No WafStack, no FrontendStack: steps 10/11 above (frontend build, S3
# upload, CloudFront invalidation) are skipped entirely — there is no S3
# bucket or CloudFront distribution to put anything into. See
# docs/DEPLOYMENT.md, "Private entry (PrivateLink), headless".
#
# Stack order, `STRATOCLAVE_ENTRY=both` (CloudFront VPC origin + PrivateLink,
# one shared internal ALB and one backend):
#   1. <Prefix>NetworkStack
#   2. <Prefix>DynamodbStack
#   3. <Prefix>EcrStack
#   4. <Prefix>AlbStack           (internal only; :80 for the VPC origin, :443 for PrivateLink)
#   5. <Prefix>WafStack           (CLOUDFRONT scope, us-east-1)
#   6. <Prefix>FrontendStack      (S3 + CloudFront, via a VPC origin)
#   7. <Prefix>PrivateLinkStack   (internal NLB + VPC endpoint service)
#   8. <Prefix>CognitoStack
#   9. <Prefix>EcsStack
#  10. <Prefix>ConfigStack
# Frontend build/upload/invalidation (steps 10/11 above) run, same as
# `cloudfront`: the web console is reachable through CloudFront in this
# mode too. See docs/DEPLOYMENT.md, "both".
#
# ECR push ordering (same in BOTH entry modes, not affected by this
# variable): this script deploys infrastructure only. The ECS service in
# either mode starts with whatever `IMAGE_TAG` tag is already in the ECR
# repository `<Prefix>EcrStack` just created — on a fresh deployment that is
# nothing, so the backend task fails its health checks until
# `scripts/build-and-push.sh` has pushed an image. Run that script AFTER
# this one, in both entry modes, exactly as "Post-deploy: container image"
# in docs/DEPLOYMENT.md describes; nothing about entry mode changes that
# ordering.
#
# NOT deployed here: the quota gated stacks (<Prefix>QuotaReconcilerStack,
# <Prefix>QuotaGrantsStack). They need the Lambda image built from
# backend/Dockerfile.lambda, which does not exist until `build-and-push.sh` has
# run once, so they are a REQUIRED second pass documented in the "Next steps"
# this script prints and in docs/DEPLOYMENT.md ("Post-deploy: quota gated
# stacks") — not an optional extra.
#
# Admin user creation is **NOT** performed here. See docs/DEPLOYMENT.md,
# "Post-deploy: first admin" (works identically in both entry modes; it is a
# Cognito API call the backend itself makes, not something a console does).
#
# Usage:
#   ./deploy-all.sh              # Deploy everything
#   ./deploy-all.sh --skip-build # Skip the Frontend build step (cloudfront mode only)
#   ./deploy-all.sh --dry-run    # Print the plan without executing
#
# Environment variables:
#   AWS_PROFILE           AWS profile (optional)
#   STRATOCLAVE_ENTRY     'cloudfront' (default), 'private', or 'both'.
#                         Switching an EXISTING deployment between these is
#                         refused by the pre-flight check below — it would
#                         replace the live ALB in place and take the
#                         deployment down. Deploy the other mode under a
#                         fresh STRATOCLAVE_PREFIX.
#   STRATOCLAVE_CLOUDFRONT_ORIGIN
#                         'public' | 'vpc-expand' | 'vpc-switch' | 'vpc'
#                         (default: 'vpc'). Only meaningful under
#                         STRATOCLAVE_ENTRY=cloudfront (refused under
#                         'private'; forced to 'vpc' under 'both'). A fresh
#                         deployment may set 'vpc' directly (the default);
#                         an EXISTING 'public' deployment must migrate
#                         through 'vpc-expand' -> 'vpc-switch' -> 'vpc', each
#                         as its own deploy — the pre-flight check below
#                         refuses a run that would skip a step. 'public'
#                         remains a supported, documented fallback (e.g. for
#                         an account whose VPC sits in an Availability Zone
#                         VPC origins do not support). See
#                         docs/DEPLOYMENT.md, "VPC origin".
#   STRATOCLAVE_REGION    Body-stack deploy region R (default: us-east-1). The
#                         WAF stack is always us-east-1 (CLOUDFRONT scope); when
#                         R != us-east-1 you must `cdk bootstrap` BOTH regions.
#   CDK_DEFAULT_REGION    Fallback for the body region if STRATOCLAVE_REGION unset
#   BEDROCK_PRIMARY_REGION Bedrock model primary region (required when R != us-east-1)
#   STRATOCLAVE_PREFIX    Resource prefix (default: stratoclave)
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IAC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$IAC_DIR/.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"
# shellcheck source=lib/cloudfront-origin.sh
source "$SCRIPT_DIR/lib/cloudfront-origin.sh"
# Body region R: the frontend S3 bucket and all queried stacks (network,
# dynamodb, ecr, alb, frontend, cognito, ecs, config) live here. Only the WAF
# stack lives in us-east-1; this script never queries it, so REGION == R.
REGION="${STRATOCLAVE_REGION:-${CDK_DEFAULT_REGION:-us-east-1}}"
PREFIX="${STRATOCLAVE_PREFIX:-stratoclave}"
# Normalised and validated IDENTICALLY to `resolveEntryConfig`
# (iac/lib/entry-config.ts): trim whitespace, lowercase, then only
# "cloudfront" or "private" are accepted. Before this, the script compared
# the RAW `STRATOCLAVE_ENTRY` string to the literal `private`, so e.g.
# `STRATOCLAVE_ENTRY=Private` passed the CDK app's own (trimmed+lowercased)
# parsing and synthesized the private stack set, while this script's
# switch-mode guard below — comparing the untouched raw string — read it as
# "not private" and let a `cdk deploy --all` through that the guard exists
# specifically to refuse. Any other value (a typo, "both") is rejected here
# the same way `resolveEntryConfig` rejects it in the app, rather than
# silently falling through to the cloudfront plan under `--dry-run`.
ENTRY="$(cfo_normalize "${STRATOCLAVE_ENTRY:-cloudfront}")"
ENTRY="${ENTRY:-cloudfront}"
cfo_validate_entry "$ENTRY" || {
  echo "[ERROR] STRATOCLAVE_ENTRY must be cloudfront, private, or both (got '${STRATOCLAVE_ENTRY}')" >&2
  exit 1
}

# Normalised and validated IDENTICALLY to `resolveCloudfrontOriginConfig`
# (iac/lib/entry-config.ts): trim whitespace, lowercase, empty-or-unset
# default is "vpc" (the ALB CloudFront talks to has no public address), then
# only the closed four-value set is accepted. Refused in the same two
# combinations the app itself refuses — ENTRY=private with anything but
# "public", ENTRY=both with anything but "vpc" — so a mistake here is a
# synth-time error from `cdk deploy`, not a silent divergence between what
# this script assumed and what the app actually built. Both the validation
# and the combo check are the SAME functions `test-cloudfront-origin-guard.sh`
# exercises directly, in `scripts/lib/cloudfront-origin.sh`.
CLOUDFRONT_ORIGIN="$(cfo_normalize "${STRATOCLAVE_CLOUDFRONT_ORIGIN:-}")"
if [ -z "$CLOUDFRONT_ORIGIN" ]; then
  CLOUDFRONT_ORIGIN="$(cfo_default_origin "$ENTRY")"
fi
cfo_validate_origin "$CLOUDFRONT_ORIGIN" || {
  echo "[ERROR] STRATOCLAVE_CLOUDFRONT_ORIGIN must be public, vpc-expand, vpc-switch, or vpc (got '${STRATOCLAVE_CLOUDFRONT_ORIGIN}')" >&2
  exit 1
}
COMBO_ERROR="$(cfo_check_combo "$ENTRY" "$CLOUDFRONT_ORIGIN")" || {
  echo "[ERROR] $COMBO_ERROR" >&2
  exit 1
}
SKIP_BUILD=false
DRY_RUN=false
DEPLOYMENT_LOG="$IAC_DIR/deploy-$(date +%Y%m%d-%H%M%S).log"

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
  case $1 in
    --skip-build) SKIP_BUILD=true; shift ;;
    --dry-run)    DRY_RUN=true; shift ;;
    --help|-h)
      sed -n '3,35p' "$0"
      exit 0
      ;;
    *)
      echo "[ERROR] Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

# --- Logging helpers ---
log_info()  { echo "[INFO]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_step()  { echo ""; echo "[STEP]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; echo "---"; }
log_error() { echo "[ERROR] $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG" >&2; }
log_warn()  { echo "[WARN]  $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }
log_ok()    { echo "[OK]    $(date +%H:%M:%S) $1" | tee -a "$DEPLOYMENT_LOG"; }

# Round-1 implementation review (Fable B2, Sol's two matching blockers): a
# plain `x=$(aws ... 2>/dev/null)` under `set -euo pipefail` aborts the WHOLE
# script the instant that `aws` call exits non-zero — and it legitimately
# does, routinely, for a "this resource does not exist yet" result such as
# `LoadBalancerNotFound` (no public ALB under the new 'vpc' default, or after
# `vpc-contract` removes it) or `ParameterNotFound` (no VPC yet, first
# deploy). `2>/dev/null` threw the real error text away, so the operator saw
# the script die with no explanation at all.
#
# This wrapper runs the AWS CLI call with `set +e` around it, captures BOTH
# the exit code and stderr, and only then decides: a recognised "absent"
# error signature prints nothing and returns 0 (success, empty result) so
# the caller's own `if [ -z "$x" ]` branches see exactly what they already
# expect; anything else is a genuine failure (wrong region, no permission,
# throttled, a real service error) and is NEVER silently treated as
# "absent" — it logs the real stderr and exits the whole script loudly,
# which `2>/dev/null` previously made impossible to tell apart from the
# routine case.
#
# Usage: `x=$(aws_lookup_or_empty "<what this is, for the error message>" \
#   aws <service> <action> --query ... --output text)`
aws_lookup_or_empty() {
  local description="$1"
  shift
  local out rc err_file
  err_file=$(mktemp)
  set +e
  out=$("$@" 2>"$err_file")
  rc=$?
  set -e
  local err
  err=$(cat "$err_file")
  rm -f "$err_file"
  if [ "$rc" -eq 0 ]; then
    printf '%s' "$out"
    return 0
  fi
  case "$err" in
    *LoadBalancerNotFound*|*TargetGroupNotFound*|*ListenerNotFound*|*ParameterNotFound*|*ParameterNotFoundException*)
      # Routine "this does not exist (yet, or any more)" — the caller's own
      # empty-string handling is the correct response, not a script abort.
      printf ''
      return 0
      ;;
    *)
      log_error "$description failed: ${err:-<aws exited $rc with no stderr>}"
      exit 1
      ;;
  esac
}

on_error() {
  local exit_code=$?
  local line_no=$1
  log_error "Deployment failed at line $line_no with exit code $exit_code"
  echo ""
  echo "Troubleshooting:"
  echo "  1. Check the log: $DEPLOYMENT_LOG"
  echo "  2. Inspect CloudFormation events:"
  echo "     aws cloudformation describe-stack-events --stack-name <STACK_NAME> --region $REGION"
  echo "  3. Retry — this script is idempotent."
  exit $exit_code
}
trap 'on_error $LINENO' ERR

# --- Stack names (kebab-case, matches iac/lib/_common.ts stackName()) ---
NETWORK_STACK="${PREFIX}-network"
DYNAMODB_STACK="${PREFIX}-dynamodb"
ECR_STACK="${PREFIX}-ecr"
ALB_STACK="${PREFIX}-alb"
FRONTEND_STACK="${PREFIX}-frontend"
PRIVATE_LINK_STACK="${PREFIX}-private-link"
COGNITO_STACK="${PREFIX}-cognito"
ECS_STACK="${PREFIX}-ecs"
CONFIG_STACK="${PREFIX}-config"
# Matches iac/lib/_common.ts::pinnedStackName(prefix, 'waf', 'us-east-1', REGION):
# the WAF stack's name only gets a region suffix when the body region is not
# its pinned region (CLOUDFRONT-scope WebACLs must live in us-east-1).
if [ "$REGION" = "us-east-1" ]; then
  WAF_STACK="${PREFIX}-waf"
else
  WAF_STACK="${PREFIX}-waf-${REGION}"
fi

get_stack_output() {
  local stack_name=$1
  local output_key=$2
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey==\`$output_key\`].OutputValue" \
    --output text 2>/dev/null
}

stack_exists() {
  local stack_name=$1
  aws cloudformation describe-stacks \
    --stack-name "$stack_name" \
    --region "$REGION" \
    --query "Stacks[0].StackStatus" \
    --output text 2>/dev/null | grep -qvE "DELETE_COMPLETE|ROLLBACK_COMPLETE"
}

deploy_stacks() {
  if [ "$DRY_RUN" = true ]; then
    log_info "[DRY RUN] Would deploy: $*"
    return 0
  fi
  cd "$IAC_DIR"
  npx cdk deploy "$@" --require-approval never 2>&1 | tee -a "$DEPLOYMENT_LOG"
}

# --- Pre-flight ---
log_step "Pre-flight checks"

ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || {
  log_error "AWS credentials not configured. Run 'aws configure' or set AWS_PROFILE."
  exit 1
}
log_info "AWS account: $ACCOUNT"
log_info "AWS region:  $REGION"
log_info "Prefix:      $PREFIX"

command -v node >/dev/null || { log_error "Node.js 22 is required (see .nvmrc; exact ranges in the engines field of frontend/package.json and iac/package.json)."; exit 1; }
log_info "Node: $(node --version)"

# Env preflight (Fable consult D3). When the body region != us-east-1, both the
# deploy AND `cdk bootstrap` (which synthesizes bin/iac.ts) require an explicit
# BEDROCK_PRIMARY_REGION — otherwise the app throws with a message that is easy
# to misread as a code bug. Surface the requirement here, before anything runs.
# Use ${VAR:-} so an UNSET var does not abort under `set -u` — this preflight
# exists precisely for the unset case, so referencing it unguarded would crash
# before the helpful message ever prints. (Fable final review B-2)
if [ "$REGION" != "us-east-1" ] && [ -z "${BEDROCK_PRIMARY_REGION:-}" ]; then
  log_error "BEDROCK_PRIMARY_REGION must be set when STRATOCLAVE_REGION ($REGION) != us-east-1."
  log_error "  It is the Bedrock MODEL region (independent of the deploy region) and is"
  log_error "  required both for this deploy and for 'cdk bootstrap' (bootstrap synths the app)."
  log_error "  Example: export BEDROCK_PRIMARY_REGION=us-east-1   # or =$REGION for in-region models"
  exit 1
fi

# Cross-region bootstrap check (Fable review M-1). When WAF is on and the body
# region != us-east-1, the WAF stack lives in us-east-1 and its cross-region
# export writer needs BOTH regions bootstrapped; a missing CDKToolkit surfaces
# as an opaque custom-resource error mid-deploy. Fail fast with a clear message.
# Skipped under --dry-run (no AWS mutation, and dry-run should not require creds
# beyond the identity check already done). (Fable final review B-2)
WAF_REGION="us-east-1"
ENABLE_WAF_EFFECTIVE="$(echo "${ENABLE_WAF:-true}" | tr '[:upper:]' '[:lower:]')"
check_bootstrap() {
  local r=$1
  aws cloudformation describe-stacks --stack-name CDKToolkit --region "$r" &>/dev/null || {
    log_error "Region $r is not cdk-bootstrapped. Run: npx cdk bootstrap aws://$ACCOUNT/$r"
    exit 1
  }
}
if [ "$DRY_RUN" != true ]; then
  check_bootstrap "$REGION"
  if [ "$ENABLE_WAF_EFFECTIVE" != "false" ] && [ "$REGION" != "$WAF_REGION" ]; then
    log_info "Body region $REGION != $WAF_REGION and WAF is on: checking us-east-1 bootstrap (cross-region WAF export)."
    check_bootstrap "$WAF_REGION"
  fi
fi

# Entry-mode switch guard. Switching STRATOCLAVE_ENTRY on an EXISTING
# deployment is not supported: AlbStack's internetFacing/internal scheme
# cannot change in place (CloudFormation replaces the load balancer), so a
# `cloudfront`-mode prefix redeployed as `private` would tear down the live
# internet-facing ALB and stand up an internal one under the same logical
# id — taking the current deployment down mid-flight, not failing cleanly.
#
# Checks the REAL, deployed ALB scheme (via `aws elbv2 describe-load-balancers`)
# rather than "does the FrontendStack/PrivateLinkStack exist": that proxy check
# missed a `cloudfront` deploy that failed before FrontendStack was reached, or
# a `private` one that failed before PrivateLinkStack was reached — both leave
# the ALB itself already created with the scheme its FIRST deploy chose, and a
# stack-existence check alone cannot see that.
#
# Runs even under --dry-run (unlike the bootstrap checks above, which skip
# dry-run because they gate a real AWS mutation): a dry-run plan must not show
# a run that the real invocation would refuse, so a mismatch here WARNS under
# --dry-run and EXITS otherwise.
check_alb_scheme_guard() {
  if ! stack_exists "$ALB_STACK"; then
    return 0
  fi
  local expected_scheme
  # `both`'s shared ALB is internal (same as `private`): there is no public
  # ALB at all under `both`, and the one it builds is never named
  # `${PREFIX}-alb` (CDK-generated name — see lib/alb-stack.ts). Checking
  # the resource literally named `${PREFIX}-alb` is still correct here: if
  # ONE exists under that name and is `internet-facing`, it is the OLD
  # public ALB from a prior `cloudfront`/`public` deployment of this same
  # prefix, which flipping straight to `both` would try to replace in
  # place exactly like the `cloudfront` <-> `private` case this guard
  # already covers.
  if [ "$ENTRY" = "private" ] || [ "$ENTRY" = "both" ]; then expected_scheme="internal"; else expected_scheme="internet-facing"; fi
  local actual_scheme
  actual_scheme=$(aws_lookup_or_empty "looking up the live ALB named '$ALB_STACK'" \
    aws elbv2 describe-load-balancers \
    --names "$ALB_STACK" --region "$REGION" \
    --query 'LoadBalancers[0].Scheme' --output text)
  if [ -z "$actual_scheme" ] || [ "$actual_scheme" = "None" ]; then
    # The ALB stack exists but the load balancer itself does not (yet, or
    # any more) — nothing to compare against.
    return 0
  fi
  if [ "$actual_scheme" != "$expected_scheme" ]; then
    local msg="STRATOCLAVE_ENTRY=$ENTRY expects the ALB scheme '$expected_scheme', but the live ALB ('$ALB_STACK') is '$actual_scheme'. Switching an existing deployment between entry modes is not supported: CloudFormation would replace the load balancer in place, taking the live deployment down rather than refusing cleanly. Deploy the other mode under a fresh STRATOCLAVE_PREFIX instead."
    if [ "$DRY_RUN" = true ]; then
      log_warn "$msg"
      log_warn "  (--dry-run does not stop here; the real invocation would refuse with exit 1.)"
    else
      log_error "$msg"
      exit 1
    fi
  fi
}
check_alb_scheme_guard

# Entry-mode switch guard, part 2 (round-1 implementation review, item 11 —
# "refuse vpc<->private and private<->both explicitly"). `check_alb_scheme_guard`
# above detects a switch by comparing the LIVE ALB's scheme against what
# this run's ENTRY expects — which works for `public` <-> `private` (an
# internet-facing ALB named "${PREFIX}-alb" vs. an internal one under the
# SAME name), but misses `STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc` entirely:
# under that state NO alb is ever named "${PREFIX}-alb" at all (only the
# internal, CDK-generated-name one exists — see lib/alb-stack.ts's
# `needsPublicAlb`), so `check_alb_scheme_guard` finds nothing to compare
# against and allows the switch through. A `cloudfront`/`vpc` deployment
# switched straight to `private` (or vice versa) would still replace
# NetworkStack's security groups and AlbStack's listeners out from under a
# live deployment exactly as unsafely as the `public` <-> `private` case
# that guard already catches — it just cannot SEE it happening.
#
# This guard uses a signal that does not depend on any ALB's name at all:
# whether FrontendStack and PrivateLinkStack exist, which is a pure
# function of which ENTRY value the LAST successful deploy used —
# `cloudfront` (any cloudfrontOrigin): FrontendStack yes, PrivateLinkStack
# no. `private`: FrontendStack no, PrivateLinkStack yes. `both`: both yes.
# Three ENTRY values, three distinct (yes,no) pairs — every switch between
# any two of them changes at least one side of the pair, so this check
# alone covers the whole matrix, including the `vpc` <-> `private` and
# `private` <-> `both` gaps `check_alb_scheme_guard` cannot see.
#
# Runs even under --dry-run, same reasoning as `check_alb_scheme_guard`.
check_entry_mode_stack_guard() {
  local frontend_exists="false" private_link_exists="false"
  if stack_exists "$FRONTEND_STACK"; then frontend_exists="true"; fi
  if stack_exists "$PRIVATE_LINK_STACK"; then private_link_exists="true"; fi
  if [ "$frontend_exists" = "false" ] && [ "$private_link_exists" = "false" ]; then
    # Neither stack has ever deployed — a fresh prefix, nothing to compare
    # this run's ENTRY against.
    return 0
  fi
  local deployed_entry=""
  if [ "$frontend_exists" = "true" ] && [ "$private_link_exists" = "false" ]; then
    deployed_entry="cloudfront"
  elif [ "$frontend_exists" = "false" ] && [ "$private_link_exists" = "true" ]; then
    deployed_entry="private"
  elif [ "$frontend_exists" = "true" ] && [ "$private_link_exists" = "true" ]; then
    deployed_entry="both"
  fi
  if [ -n "$deployed_entry" ] && [ "$deployed_entry" != "$ENTRY" ]; then
    local msg="STRATOCLAVE_ENTRY=$ENTRY was requested, but this prefix's last successful deploy used STRATOCLAVE_ENTRY=$deployed_entry (FrontendStack exists: $frontend_exists, PrivateLinkStack exists: $private_link_exists). Switching entry mode on an existing deployment is not supported for ANY pair of modes — cloudfront<->private, cloudfront<->both, AND private<->both all replace NetworkStack's security groups and AlbStack's listeners out from under a live deployment rather than failing cleanly. Deploy STRATOCLAVE_ENTRY=$ENTRY under a fresh STRATOCLAVE_PREFIX instead. See docs/DEPLOYMENT.md's entry-mode section."
    if [ "$DRY_RUN" = true ]; then
      log_warn "$msg"
      log_warn "  (--dry-run does not stop here; the real invocation would refuse with exit 1.)"
    else
      log_error "$msg"
      exit 1
    fi
  fi
}
check_entry_mode_stack_guard

# cloudfrontOrigin migration guard. ENTRY=cloudfront's PUBLIC ALB is ALWAYS
# named exactly "${PREFIX}-alb" when it exists (lib/alb-stack.ts's primary
# build sets `loadBalancerName` explicitly); the INTERNAL ALB never is
# (CDK-generated name). So a load balancer found under that literal name,
# reporting `internet-facing`, means "this deployment currently has a live
# public ALB" regardless of what CLOUDFRONT_ORIGIN this run is about to set
# it to — which is exactly the fact this guard needs.
#
# Refuses a run that would, in one step, both remove that live public ALB
# AND stand up the internal one for the first time — the unsafe one-shot
# replacement the vpc-expand -> vpc-switch -> vpc staging exists to avoid
# (see docs/DEPLOYMENT.md's migration section). Whether the internal ALB
# already exists (meaning vpc-expand/vpc-switch already ran at least once)
# is read from AlbStack's own `InternalAlbDnsName` CloudFormation output,
# not re-derived — that output exists if and only if lib/alb-stack.ts's
# internal-ALB branch built one on the LAST successful deploy.
#
# Runs even under --dry-run, same reasoning as `check_alb_scheme_guard`
# above: a dry-run plan must not show a run the real invocation would
# refuse.
# The actual AWS-querying wrapper; the decision itself is
# `cfo_migration_decision` (scripts/lib/cloudfront-origin.sh), tested there
# directly with no AWS calls at all.
check_cloudfront_origin_migration_guard() {
  if [ "$ENTRY" != "cloudfront" ]; then
    # `private` has no CloudFront origin at all; `both` is refused above
    # unless CLOUDFRONT_ORIGIN=vpc, and a `cloudfront` -> `both` flip is
    # already caught by `check_alb_scheme_guard`'s own ENTRY-switch logic
    # (both resolve to an "internal" expected scheme).
    return 0
  fi
  if ! stack_exists "$ALB_STACK"; then
    return 0
  fi
  local live_public_scheme
  live_public_scheme=$(aws_lookup_or_empty "looking up the live public ALB named '${PREFIX}-alb'" \
    aws elbv2 describe-load-balancers \
    --names "${PREFIX}-alb" --region "$REGION" \
    --query 'LoadBalancers[0].Scheme' --output text)
  local live_internal_dns live_internal_alb_present
  live_internal_dns=$(get_stack_output "$ALB_STACK" "InternalAlbDnsName")
  if [ -n "$live_internal_dns" ]; then live_internal_alb_present="true"; else live_internal_alb_present="false"; fi

  # The two facts `cfo_migration_decision` needs beyond "did vpc-expand/
  # vpc-switch ever run at all" (round-1 implementation review, Fable
  # should-fix 2 / Sol's matching should-fix: presence of the internal ALB
  # alone let a forgotten env var skip straight from vpc-expand to vpc).
  # Both read the LIVE CloudFront distribution, not CDK's synthesized idea
  # of it, so a switch that is still mid-propagation or a contraction that
  # has not actually deployed cannot be mistaken for one that has.
  local live_switch_deployed="false" live_public_origin_declared="false"
  if [ "$live_internal_alb_present" = "true" ] && stack_exists "$FRONTEND_STACK"; then
    local dist_id
    dist_id=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDistributionId")
    if [ -n "$dist_id" ]; then
      local dist_status target_origin_id
      dist_status=$(aws_lookup_or_empty "looking up CloudFront distribution '$dist_id' status" \
        aws cloudfront get-distribution --id "$dist_id" \
        --query 'Distribution.Status' --output text)
      target_origin_id=$(aws_lookup_or_empty "looking up CloudFront distribution '$dist_id' behaviours" \
        aws cloudfront get-distribution-config --id "$dist_id" \
        --query 'DistributionConfig.CacheBehaviors.Items[0].TargetOriginId' --output text)
      if [ "$dist_status" = "Deployed" ] && [ "$target_origin_id" = "ALBVpcOrigin" ]; then
        live_switch_deployed="true"
      fi
      local public_origin_count
      public_origin_count=$(aws_lookup_or_empty "looking up CloudFront distribution '$dist_id' origins" \
        aws cloudfront get-distribution-config --id "$dist_id" \
        --query "length(DistributionConfig.Origins.Items[?Id=='ALBOrigin'])" --output text)
      if [ "$public_origin_count" != "0" ] && [ -n "$public_origin_count" ]; then
        live_public_origin_declared="true"
      fi
    fi
  fi

  local msg
  if msg="$(cfo_migration_decision "$CLOUDFRONT_ORIGIN" "$live_public_scheme" "$live_internal_alb_present" "$live_switch_deployed" "$live_public_origin_declared")"; then
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    log_warn "$msg"
    log_warn "  (--dry-run does not stop here; the real invocation would refuse with exit 1.)"
  else
    log_error "$msg"
    exit 1
  fi
}
check_cloudfront_origin_migration_guard

# AZ pre-flight (vpc-ish cloudfrontOrigin modes only: VPC origins are
# unsupported in a handful of specific AZ IDs — use1-az3, apne1-az3,
# usw1-az2, cac1-az3 — and the VPC's own AZ choice is NEVER held in shell
# env (holding it here would be the exact "pinned value an operator can
# forget to set on a later deploy" failure mode the abandoned
# secret-header design kept reproducing). This checks the AZ IDs the
# subnets the internal ALB will actually use resolve to, and refuses with
# the fix (pin AZs in committed CDK context) rather than letting a VPC
# origin fail to create — or, worse, create successfully in a supported AZ
# while the NEW private-isolated subnets landed in a DIFFERENT, unsupported
# one than an operator assumed.
#
# Skipped entirely for STRATOCLAVE_CLOUDFRONT_ORIGIN=public (no VPC origin
# built at all in that mode). Round-1 implementation review (both reviews'
# matching should-fix): this check used to be skipped under --dry-run too
# ("no AWS mutation is about to happen") — but it is READ-ONLY, so a
# `--dry-run` plan telling an operator a run is safe while silently having
# skipped the one check that would have refused it is worse than running
# it; it now runs unconditionally. It also used to WARN and continue (fail
# OPEN) on a lookup error or on finding zero subnets — fixed to fail CLOSED
# (refuse the deploy) on either, below.
check_vpc_origin_az_support() {
  if [ "$CLOUDFRONT_ORIGIN" = "public" ]; then
    return 0
  fi
  local vpc_id
  vpc_id=$(aws_lookup_or_empty "looking up the existing VPC for prefix '$PREFIX'" \
    aws ssm get-parameter --name "/${PREFIX}/network/vpc-id" \
    --region "$REGION" --query 'Parameter.Value' --output text)
  local az_ids=""
  if [ -z "$vpc_id" ] || [ "$vpc_id" = "None" ]; then
    # FRESH deployment: no VPC exists yet, so there is no live subnet to
    # read an AZ ID off of — but there is still something to check, because
    # an operator CAN still choose AZs before the first deploy (pinning
    # them in committed CDK context), which is the one case a post-hoc
    # check cannot undo. Replicate CDK's own default AZ selection for a
    # `cdk.json` with no pinned `availability-zones:...` context key and no
    # explicit `maxAzs` override: the CDK AZ context provider queries
    # EC2 DescribeAvailabilityZones for the account/region, sorts the
    # results by zone NAME, and `ec2.Vpc`'s own default `maxAzs: 2`
    # (lib/network-stack.ts) takes the first two of that sorted list.
    log_info "No existing VPC found for prefix '$PREFIX' (first deploy) — resolving the AZs a fresh 'cdk deploy' would pick by replicating CDK's own default AZ selection (first 2, sorted by zone name)."
    az_ids=$(aws_lookup_or_empty "resolving this account/region's Availability Zones for a fresh deploy" \
      aws ec2 describe-availability-zones --region "$REGION" \
      --filters 'Name=zone-type,Values=availability-zone' \
      --query 'sort_by(AvailabilityZones, &ZoneName)[:2].ZoneId' --output text)
  else
    # EXISTING deployment: read the AZ IDs off the PRIVATE_ISOLATED subnets
    # directly when they exist (round-1 implementation review: the previous
    # version checked the PUBLIC subnets' zone NAMES as a proxy for "the
    # internal ALB's own subnets", rather than the isolated subnets the
    # internal ALB actually lives in). `AvailabilityZoneId` is returned
    # directly by `describe-subnets` — no second describe-availability-zones
    # round trip needed to turn a zone NAME into an ID the way the old
    # per-public-subnet loop required. Falls back to the public subnets'
    # AZs only when no isolated subnets exist yet (this deployment has
    # never run a non-'public' cloudfrontOrigin before) — the VPC's AZ
    # choice is the same regardless of which subnet group is queried, once
    # the VPC itself exists (see lib/network-stack.ts's doc on why
    # appending the PrivateIsolated group cannot move the VPC's own AZs).
    az_ids=$(aws_lookup_or_empty "looking up this VPC's PrivateIsolated subnet AZs" \
      aws ec2 describe-subnets --region "$REGION" \
      --filters "Name=vpc-id,Values=$vpc_id" "Name=tag:aws-cdk:subnet-type,Values=Isolated" \
      --query 'Subnets[].AvailabilityZoneId' --output text)
    if [ -z "$az_ids" ]; then
      az_ids=$(aws_lookup_or_empty "looking up this VPC's Public subnet AZs" \
        aws ec2 describe-subnets --region "$REGION" \
        --filters "Name=vpc-id,Values=$vpc_id" "Name=tag:aws-cdk:subnet-type,Values=Public" \
        --query 'Subnets[].AvailabilityZoneId' --output text)
    fi
  fi
  if [ -z "$az_ids" ]; then
    # Fail CLOSED, not open: a previous version warned and continued here,
    # which is indistinguishable from "checked and found nothing wrong" to
    # an operator reading the log — round-1 implementation review (both
    # reviews' matching should-fix).
    log_error "Could not resolve any Availability Zone for this deployment's VPC (prefix '$PREFIX', region $REGION) — refusing rather than silently skipping the VPC-origin AZ check. Verify manually before retrying: aws ec2 describe-subnets --region $REGION --filters Name=vpc-id,Values=$vpc_id"
    exit 1
  fi
  local az_id unsupported_found=false
  for az_id in $az_ids; do
    if cfo_az_unsupported "$az_id"; then
      log_error "This deployment's VPC uses Availability Zone ID $az_id — CloudFront VPC origins are unsupported there."
      unsupported_found=true
    fi
  done
  if [ "$unsupported_found" = true ]; then
    log_error "STRATOCLAVE_CLOUDFRONT_ORIGIN=$CLOUDFRONT_ORIGIN needs a VPC whose Availability Zones are all VPC-origin-supported. This account's VPC for prefix '$PREFIX' is not. Fix: pin supported AZs in COMMITTED CDK context (cdk.json's 'availability-zones:account=<account>:region=<region>' key — never in shell env, which an operator can forget to set on a later deploy), then deploy a FRESH prefix (an existing VPC's AZs cannot be changed without replacing its subnets). STRATOCLAVE_CLOUDFRONT_ORIGIN=public remains the supported, documented fallback for an account whose VPC sits in an unsupported AZ and cannot be moved."
    exit 1
  fi
  log_info "VPC-origin AZ pre-flight: all of this deployment's AZs support CloudFront VPC origins."
}
check_vpc_origin_az_support

if [ ! -d "$IAC_DIR/node_modules" ]; then
  log_info "Installing CDK dependencies..."
  cd "$IAC_DIR" && npm install
fi

log_info "Type-checking CDK..."
cd "$IAC_DIR" && npx tsc --noEmit 2>&1 | tee -a "$DEPLOYMENT_LOG" || {
  log_error "TypeScript compilation failed. Fix errors in iac/lib/ before retrying."
  exit 1
}

if [ "$DRY_RUN" = true ]; then
  echo ""
  echo "Plan (STRATOCLAVE_ENTRY=$ENTRY, STRATOCLAVE_CLOUDFRONT_ORIGIN=$CLOUDFRONT_ORIGIN):"
  echo "  1. $NETWORK_STACK"
  echo "  2. $DYNAMODB_STACK"
  echo "  3. $ECR_STACK"
  echo "  4. $ALB_STACK"
  if [ "$ENTRY" = "private" ]; then
    echo "  5. $PRIVATE_LINK_STACK"
    echo "  6. $COGNITO_STACK"
    echo "  7. $ECS_STACK"
    echo "  8. $CONFIG_STACK"
    echo "  (no WAF stack, no Frontend stack: headless, no CloudFront, no S3 console)"
  elif [ "$ENTRY" = "both" ]; then
    if [ "$ENABLE_WAF_EFFECTIVE" != "false" ]; then
      echo "  5. $WAF_STACK"
    fi
    echo "  6. $FRONTEND_STACK      (via a CloudFront VPC origin onto the internal ALB)"
    echo "  7. $PRIVATE_LINK_STACK"
    echo "  8. $COGNITO_STACK"
    echo "  9. $ECS_STACK"
    echo "  10. $CONFIG_STACK"
    if [ "$SKIP_BUILD" = false ]; then
      echo "  11. Build Frontend, upload to S3, invalidate CloudFront"
    fi
  else
    if [ "$ENABLE_WAF_EFFECTIVE" != "false" ]; then
      echo "  5. $WAF_STACK"
    fi
    echo "  6. $FRONTEND_STACK"
    echo "  7. $COGNITO_STACK"
    echo "  8. $ECS_STACK"
    echo "  9. $CONFIG_STACK"
    if [ "$SKIP_BUILD" = false ]; then
      echo "  10. Build Frontend, upload to S3, invalidate CloudFront"
    fi
  fi
  exit 0
fi

# --- Deploy all stacks in one shot (CDK resolves dependencies automatically) ---
log_step "Deploying all CDK stacks"
deploy_stacks --all
log_ok "All stacks deployed"

# --- Build and deploy frontend (cloudfront mode only: private mode has no
# CloudFront distribution or S3 console bucket to build or upload anything
# to) ---
if [ "$ENTRY" = "private" ]; then
  log_info "STRATOCLAVE_ENTRY=private: no frontend build/upload/invalidation (headless; no web console on this path)."
elif [ "$SKIP_BUILD" = true ]; then
  log_warn "Skipping Frontend build (--skip-build)"
else
  log_step "Building and deploying Frontend assets"

  COGNITO_CLIENT_ID=$(get_stack_output "$COGNITO_STACK" "UserPoolClientId")
  COGNITO_USER_POOL_ID=$(get_stack_output "$COGNITO_STACK" "UserPoolId")
  COGNITO_DOMAIN=$(get_stack_output "$COGNITO_STACK" "CognitoDomain")
  CLOUDFRONT_DOMAIN=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")
  FRONTEND_BUCKET=$(get_stack_output "$FRONTEND_STACK" "FrontendBucketName")
  DISTRIBUTION_ID=$(get_stack_output "$FRONTEND_STACK" "CloudFrontDistributionId")

  for var in COGNITO_CLIENT_ID COGNITO_USER_POOL_ID COGNITO_DOMAIN CLOUDFRONT_DOMAIN FRONTEND_BUCKET DISTRIBUTION_ID; do
    if [ -z "${!var}" ]; then
      log_error "Stack output $var is empty. Check that all stacks deployed successfully."
      exit 1
    fi
  done

  log_info "CloudFront domain: $CLOUDFRONT_DOMAIN"
  log_info "Cognito client ID: $COGNITO_CLIENT_ID"
  log_info "Frontend bucket:   $FRONTEND_BUCKET"

  cd "$FRONTEND_DIR"
  [ -d node_modules ] || npm ci 2>&1 | tee -a "$DEPLOYMENT_LOG"
  npm run build 2>&1 | tee -a "$DEPLOYMENT_LOG"

  if [ ! -d dist ]; then
    log_error "Frontend build failed: dist/ not produced"
    exit 1
  fi

  log_info "Generating dist/config.json from stack outputs"
  cat > dist/config.json <<CONFIGEOF
{
  "cognito": {
    "user_pool_id": "$COGNITO_USER_POOL_ID",
    "client_id": "$COGNITO_CLIENT_ID",
    "domain": "$COGNITO_DOMAIN",
    "region": "$REGION"
  },
  "api": {
    "endpoint": ""
  },
  "app": {
    "cloudfront_domain": "$CLOUDFRONT_DOMAIN"
  }
}
CONFIGEOF

  aws s3 sync dist/ "s3://$FRONTEND_BUCKET/" --delete --region "$REGION" \
    2>&1 | tee -a "$DEPLOYMENT_LOG"

  aws cloudfront create-invalidation \
    --distribution-id "$DISTRIBUTION_ID" \
    --paths "/*" \
    --region "$REGION" \
    --output json 2>&1 | tee -a "$DEPLOYMENT_LOG"

  log_ok "Frontend built and deployed"
fi

# --- Summary ---
# "AlbDnsName" only exists when a public ALB was built (cloudfrontOrigin
# public/vpc-expand/vpc-switch); "InternalAlbDnsName" only when an internal
# one was (vpc-expand/vpc-switch/vpc, or any STRATOCLAVE_ENTRY=both deploy).
# At least one always exists.
ALB_DNS=$(get_stack_output "$ALB_STACK" "AlbDnsName" 2>/dev/null || echo "")
if [ -z "$ALB_DNS" ]; then
  ALB_DNS=$(get_stack_output "$ALB_STACK" "InternalAlbDnsName" 2>/dev/null || echo "N/A")
fi
COGNITO_USER_POOL_ID=${COGNITO_USER_POOL_ID:-$(get_stack_output "$COGNITO_STACK" "UserPoolId" 2>/dev/null || echo "N/A")}

echo ""
echo "============================================"
echo "[SUCCESS] Deployment completed"
echo "============================================"
echo ""

if [ "$ENTRY" = "both" ]; then
  CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")}
  PRIVATE_URL=$(get_stack_output "$PRIVATE_LINK_STACK" "PrivateUrl" 2>/dev/null || echo "N/A")
  ENDPOINT_SERVICE_NAME=$(get_stack_output "$PRIVATE_LINK_STACK" "EndpointServiceName" 2>/dev/null || echo "N/A")
  echo "  Frontend URL (WAF-protected): https://$CLOUDFRONT_DOMAIN"
  echo "  Private URL (no WAF):         $PRIVATE_URL"
  echo "  Endpoint service:             $ENDPOINT_SERVICE_NAME"
  echo "  ALB (internal, shared):       $ALB_DNS"
  echo "  User Pool ID:                 $COGNITO_USER_POOL_ID"
  echo ""
  echo "Next steps (STRATOCLAVE_ENTRY=both):"
  echo "  1. Push the Backend container image (also builds+pushes the scheduled-Lambda"
  echo "     image and prints the LAMBDA_IMAGE_TAG the next step needs):"
  echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
  echo "  2. REQUIRED second pass — deploy the quota gated stacks:"
  echo "     cd $IAC_DIR && LAMBDA_IMAGE_TAG=<tag from step 1> npx cdk deploy \\"
  echo "       ${PREFIX}-quota-reconciler ${PREFIX}-quota-grants \\"
  echo "       -c quotaReconciler=true -c quotaGrants=true --require-approval never"
  echo "  3. Share the CloudFront URL with WAF-protected / general CLI users:"
  echo "     stratoclave setup https://$CLOUDFRONT_DOMAIN"
  echo "  4. Have each approved PrivateLink consumer create their interface endpoint,"
  echo "     accept the resulting pending connection(s) from this (provider) account,"
  echo "     then point STRATOCLAVE_PRIVATE_DOMAIN at the endpoint from inside the"
  echo "     consumer VPC — same exchange as STRATOCLAVE_ENTRY=private. See"
  echo "     docs/DEPLOYMENT.md, 'both', and 'Private entry (PrivateLink), headless'."
  echo "     Only the PrivateLink path bypasses WAF; the CloudFront path above does not."
elif [ "$ENTRY" = "private" ]; then
  PRIVATE_URL=$(get_stack_output "$PRIVATE_LINK_STACK" "PrivateUrl" 2>/dev/null || echo "N/A")
  ENDPOINT_SERVICE_NAME=$(get_stack_output "$PRIVATE_LINK_STACK" "EndpointServiceName" 2>/dev/null || echo "N/A")
  echo "  Private URL:       $PRIVATE_URL"
  echo "  Endpoint service:  $ENDPOINT_SERVICE_NAME"
  echo "  ALB (internal):    $ALB_DNS"
  echo "  User Pool ID:      $COGNITO_USER_POOL_ID"
  echo ""
  echo "Next steps (STRATOCLAVE_ENTRY=private):"
  echo "  1. Push the Backend container image (same ordering as cloudfront mode:"
  echo "     this runs after the stacks above regardless of entry mode; also"
  echo "     builds+pushes the scheduled-Lambda image and prints the"
  echo "     LAMBDA_IMAGE_TAG the quota-stack pass below needs):"
  echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
  echo "  2. REQUIRED second pass — deploy the quota gated stacks (skipping this leaves"
  echo "     limit-raise grants that never expire and tenant pools that vanish on the"
  echo "     1st for any tenant whose membership did not change that month):"
  echo "     cd $IAC_DIR && LAMBDA_IMAGE_TAG=<tag from step 1> npx cdk deploy \\"
  echo "       ${PREFIX}-quota-reconciler ${PREFIX}-quota-grants \\"
  echo "       -c quotaReconciler=true -c quotaGrants=true --require-approval never"
  echo "  3. Have each approved consumer create their interface endpoint first, then"
  echo "     accept the resulting pending connection(s) from this (provider) account,"
  echo "     then point STRATOCLAVE_PRIVATE_DOMAIN at the endpoint from inside the"
  echo "     consumer VPC. See docs/DEPLOYMENT.md, 'Private entry (PrivateLink),"
  echo "     headless' for every step of that exchange."
  echo "  4. From a host inside an accepted consumer VPC, point the CLI at this"
  echo "     deployment, then create the first admin/API key without a console:"
  echo "     stratoclave setup https://<your STRATOCLAVE_PRIVATE_DOMAIN>"
  echo "     See docs/DEPLOYMENT.md, 'Private entry (PrivateLink), headless', the"
  echo "     'Bootstrapping without a console' subsection, for the rest."
else
  CLOUDFRONT_DOMAIN=${CLOUDFRONT_DOMAIN:-$(get_stack_output "$FRONTEND_STACK" "CloudFrontDomainName")}
  echo "  Frontend URL:  https://$CLOUDFRONT_DOMAIN"
  if [ "$CLOUDFRONT_ORIGIN" = "public" ]; then
    echo "  ALB endpoint:  http://$ALB_DNS   (public; reachable directly, not just through CloudFront — see docs/DEPLOYMENT.md's 'VPC origin' section for why STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc closes that)"
  else
    echo "  ALB (internal, no public address): $ALB_DNS   (reachable only through the CloudFront VPC origin above or from inside this VPC)"
  fi
  echo "  User Pool ID:  $COGNITO_USER_POOL_ID"
  echo ""
  echo "Next steps:"
  echo "  1. Push the Backend container image (also builds+pushes the scheduled-Lambda"
  echo "     image and prints the LAMBDA_IMAGE_TAG the next step needs):"
  echo "     cd $IAC_DIR && ./scripts/build-and-push.sh"
  echo "  2. REQUIRED second pass — deploy the quota gated stacks (skipping this leaves"
  echo "     limit-raise grants that never expire and tenant pools that vanish on the"
  echo "     1st for any tenant whose membership did not change that month):"
  echo "     cd $IAC_DIR && LAMBDA_IMAGE_TAG=<tag from step 1> npx cdk deploy \\"
  echo "       ${PREFIX}-quota-reconciler ${PREFIX}-quota-grants \\"
  echo "       -c quotaReconciler=true -c quotaGrants=true --require-approval never"
  echo "  3. Create the first admin user (bootstrap-admin.sh is stale — see"
  echo "     docs/DEPLOYMENT.md 'Post-deploy: first admin'):"
  echo "     export STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL=admin@example.com"
  echo "     cd $IAC_DIR && npx cdk deploy ${PREFIX}-ecs --require-approval never"
  echo "     aws secretsmanager get-secret-value --secret-id ${PREFIX}/bootstrap-admin-temp-password --query SecretString --output text"
  echo "  4. Share the CloudFront URL with CLI users:"
  echo "     stratoclave setup https://$CLOUDFRONT_DOMAIN"
fi
echo ""
echo "Log: $DEPLOYMENT_LOG"
