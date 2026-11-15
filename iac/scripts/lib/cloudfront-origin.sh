#!/bin/bash
# Pure decision functions for STRATOCLAVE_ENTRY / STRATOCLAVE_CLOUDFRONT_ORIGIN
# parsing and the vpc-origin migration guard, factored out of
# scripts/deploy-all.sh so they can be unit-tested with plain string/exit-code
# assertions (test-cloudfront-origin-guard.sh) instead of needing a stubbed
# `aws` CLI to drive the whole deploy script end to end.
#
# Every function here takes its inputs as ARGUMENTS, never reads an env var
# or calls `aws` itself — deploy-all.sh resolves those (env, `aws` calls) and
# passes the resolved values in. That split is what makes this file testable
# without AWS credentials or a network call, and keeps `aws`-calling code in
# exactly one place (deploy-all.sh), not duplicated between this file and a
# test harness's stub.
#
# No function here calls `exit`: each returns 0 (allow / valid) or 1 (refuse
# / invalid), printing an explanatory message to stdout when it returns 1, so
# the caller decides whether a 1 is a hard failure (deploy-all.sh) or just an
# assertion (the test script).

# Trim LEADING/TRAILING whitespace and lowercase. Same operation for
# STRATOCLAVE_ENTRY and STRATOCLAVE_CLOUDFRONT_ORIGIN, same convention as
# iac/lib/entry-config.ts's `.trim().toLowerCase()` — reused here so the two
# can never parse a value differently.
#
# Round-1 implementation review (Sol): an earlier version used
# `tr -d '[:space:]'`, which deletes EVERY whitespace character, including
# internal ones — `"vpc -expand"` normalized to `"vpc-expand"`, a value
# TypeScript's `.trim()` leaves as `"vpc -expand"` (internal space intact)
# and therefore refuses, since it is not an exact match for any of
# `resolveCloudfrontOriginConfig`'s closed set. The shell wrapper's
# pre-flight guards would have WAVED THROUGH a value `cdk synth` itself then
# refused — the wrapper approving something CDK rejects is exactly the
# parity gap a pre-flight check exists to prevent. Trimming only the edges
# (`sed`'s leading/trailing whitespace pattern) leaves an internal space
# in place, so `cfo_validate_entry`/`cfo_validate_origin`'s exact-string
# `case` patterns below refuse it the same way TypeScript's `===` does —
# no separate "reject internal whitespace" check needed; the existing
# closed-set match already is one.
cfo_normalize() {
  printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]'
}

# 0 if `$1` (already normalized) is a valid STRATOCLAVE_ENTRY value.
cfo_validate_entry() {
  case "$1" in
    cloudfront|private|both) return 0 ;;
    *) return 1 ;;
  esac
}

# The SILENT default for STRATOCLAVE_CLOUDFRONT_ORIGIN when unset/empty,
# given `$1` = the already-normalized STRATOCLAVE_ENTRY. Mirrors
# `defaultCloudfrontOrigin` in iac/lib/entry-config.ts exactly: "private" has
# no CloudFront distribution at all, so defaulting it to anything but
# "public" would make every pre-existing `STRATOCLAVE_ENTRY=private`
# deployment that never set this variable fail the moment this default
# changed. An EXPLICIT value is still validated/combo-checked as written —
# this function only decides what UNSET quietly means.
cfo_default_origin() {
  if [ "$1" = "private" ]; then
    echo "public"
  else
    echo "vpc"
  fi
}

# 0 if `$1` (already normalized) is a valid STRATOCLAVE_CLOUDFRONT_ORIGIN value.
cfo_validate_origin() {
  case "$1" in
    public|vpc-expand|vpc-switch|vpc-contract|vpc) return 0 ;;
    *) return 1 ;;
  esac
}

# 0 if the (entry, origin) combination is allowed; 1 (with a message on
# stdout) otherwise. Mirrors `resolveCloudfrontOriginConfig` in
# iac/lib/entry-config.ts exactly: `private` only ever takes `public`,
# `both` only ever takes `vpc`.
cfo_check_combo() {
  local entry="$1" origin="$2"
  if [ "$entry" = "private" ] && [ "$origin" != "public" ]; then
    echo "STRATOCLAVE_CLOUDFRONT_ORIGIN=$origin has no effect under STRATOCLAVE_ENTRY=private (no CloudFront distribution on that path). Unset it, or set it to 'public'."
    return 1
  fi
  if [ "$entry" = "both" ] && [ "$origin" != "vpc" ]; then
    echo "STRATOCLAVE_CLOUDFRONT_ORIGIN=$origin is refused under STRATOCLAVE_ENTRY=both (that mode keeps exactly one ALB, internal only; there is no public ALB for 'public'/'vpc-expand'/'vpc-switch' to describe a state of). Unset it, or set it to 'vpc'."
    return 1
  fi
  return 0
}

# The vpc-origin migration guard's decision, given already-resolved facts:
#   $1 origin                       the normalized STRATOCLAVE_CLOUDFRONT_ORIGIN for THIS run
#   $2 live_public_scheme           `aws elbv2 describe-load-balancers --names "<prefix>-alb" ...Scheme`,
#                                   or "" if no such load balancer exists
#   $3 live_internal_alb_present    "true" if AlbStack's InternalAlbDnsName output is
#                                   already non-empty (a prior vpc-expand/vpc-switch/
#                                   vpc-contract deploy already ran), "false" otherwise
#   $4 live_switch_deployed         "true" if the LIVE CloudFront distribution's
#                                   behaviours already target the VPC origin (not the
#                                   public one) AND the distribution's own Status is
#                                   "Deployed" (not still propagating a prior change);
#                                   "false" otherwise. Only meaningful once
#                                   live_internal_alb_present is "true".
#   $5 live_public_origin_declared  "true" if the LIVE distribution still declares an
#                                   origin with id "ALBOrigin" at all (round-1
#                                   implementation review: this is "true" through
#                                   public/vpc-expand/vpc-switch and becomes "false"
#                                   only once vpc-contract has actually deployed —
#                                   FrontendStack stops declaring that origin in that
#                                   step, not merely retargeting away from it); "false"
#                                   when it is gone, or when there has never been a
#                                   public ALB at all (fresh deployment).
#
# Returns 0 (allow) or 1 (refuse, with a message on stdout).
cfo_migration_decision() {
  local origin="$1" live_public_scheme="$2" live_internal_alb_present="$3"
  local live_switch_deployed="$4" live_public_origin_declared="$5"
  local migrating=true

  if [ -z "$live_public_scheme" ] || [ "$live_public_scheme" = "None" ]; then
    migrating=false
  elif [ "$live_public_scheme" != "internet-facing" ]; then
    migrating=false
  fi

  case "$origin" in
    public|vpc-expand)
      return 0
      ;;
    vpc-switch)
      if [ "$migrating" != true ]; then
        return 0
      fi
      if [ "$live_internal_alb_present" != "true" ]; then
        echo "STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-switch assumes the internal ALB from a prior vpc-expand deploy already exists, but this deployment's AlbStack has no InternalAlbDnsName output yet. Run STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-expand first, as its own deploy, then retry vpc-switch. See docs/DEPLOYMENT.md, 'Migrating an existing public deployment to a VPC origin'."
        return 1
      fi
      return 0
      ;;
    vpc-contract)
      if [ "$migrating" != true ]; then
        return 0
      fi
      if [ "$live_switch_deployed" != "true" ]; then
        echo "STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-contract assumes a prior vpc-switch deploy has ALREADY retargeted this distribution's behaviours to the VPC origin AND finished propagating (CloudFront distribution Status=Deployed) — neither is true yet (live_switch_deployed=$live_switch_deployed). Run STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-switch first, confirm with 'aws cloudfront get-distribution --id <id> --query Distribution.Status' that it reads Deployed, then retry vpc-contract. Removing FrontendStack's/EcsStack's consumption of the public ALB before the VPC origin path is confirmed live would cut over to an unverified origin. See docs/DEPLOYMENT.md's migration section."
        return 1
      fi
      return 0
      ;;
    vpc)
      if [ "$migrating" != true ]; then
        return 0
      fi
      if [ "$live_public_origin_declared" = "true" ]; then
        echo "A live internet-facing ALB exists and this distribution still declares the public ALBOrigin (vpc-contract has not deployed successfully yet, or was skipped). STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc (also the default when unset) would make AlbStack delete the public ALB's exports in the SAME deploy where FrontendStack/EcsStack still import them — CloudFormation refuses that outright ('Export ... cannot be deleted as it is in use by ...'). Run the four steps in order, each as its own deploy: STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc-expand, then =vpc-switch, then =vpc-contract, then =vpc. A FRESH deployment with no existing public ALB is unaffected by this guard and may set vpc directly. See docs/DEPLOYMENT.md, 'Migrating an existing public deployment to a VPC origin'."
        return 1
      fi
      return 0
      ;;
    *)
      echo "cfo_migration_decision: unknown origin '$origin'"
      return 1
      ;;
  esac
}

# The fixed set of Availability Zone IDs CloudFront VPC origins do not
# support (docs/DEPLOYMENT.md's "VPC origin" section cites the AWS docs for
# this). 0 (unsupported) if `$1` is one of them, 1 (supported) otherwise.
cfo_az_unsupported() {
  case "$1" in
    use1-az3|apne1-az3|usw1-az2|cac1-az3) return 0 ;;
    *) return 1 ;;
  esac
}
