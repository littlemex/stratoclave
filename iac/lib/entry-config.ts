/**
 * Pure entry-mode resolution for the Stratoclave app entrypoint.
 *
 * Same shape as `lib/region-config.ts`: a pure function over a plain env
 * object (plus the already-resolved body region, since the certificate
 * check below needs it), so it is unit-testable in-process with no `cdk
 * synth` subprocess, and `bin/iac.ts` calls it once and wires the result
 * into which stacks get created and how.
 *
 * `STRATOCLAVE_ENTRY` selects how the gateway is reached from outside its own
 * VPC:
 *
 *   - `cloudfront` (default, and also when unset or empty): CloudFront + S3
 *     console in front of the ALB. See `resolveCloudfrontOriginConfig` below
 *     for whether that ALB has a public address at all.
 *   - `private`: no CloudFront, no S3 console, no CLOUDFRONT-scope WAF. An
 *     internal ALB reached only through AWS PrivateLink (an internal NLB plus
 *     a VPC endpoint service). Requires an operator-supplied ACM certificate
 *     (in the same region as the deployment), a bare domain name, and a
 *     non-empty, non-wildcard allowed-principals list — all three are
 *     refused at synth, loudly, when missing or malformed, because an
 *     internal entry point with no certificate or no allowed principal is
 *     not a smaller version of the feature, it is a different and broken
 *     one.
 *   - `both`: CloudFront (via a VPC origin) AND PrivateLink share ONE
 *     internal ALB and one backend. Requires the SAME three variables as
 *     `private` (the operator's PrivateLink leg needs its own certificate,
 *     domain, and allowed-principals list regardless of CloudFront also
 *     being in front) — parsed by the same `resolvePrivateEntryConfig`
 *     helper `private` uses, so the two modes cannot drift on what counts as
 *     valid. `lib/alb-stack.ts` refuses to build a public ALB at all in this
 *     mode: CloudFront reaches the shared internal ALB only through a VPC
 *     origin (`resolveCloudfrontOriginConfig` below forces `'vpc'`), because
 *     a public ALB alongside PrivateLink would defeat the reason PrivateLink
 *     exists in the first place.
 */

export interface PrivateEntryConfig {
  /** `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN` — an ACM certificate for the ALB's HTTPS listener, in the deployment's own region. */
  readonly certificateArn: string;
  /** `STRATOCLAVE_PRIVATE_DOMAIN` — a bare hostname, e.g. `stratoclave.internal.example.com`. */
  readonly domainName: string;
  /**
   * `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` — IAM principal ARNs allowed to
   * request a connection to the endpoint service (the provider — this
   * deployment's operator — still has to accept each one; being allowed is
   * not being auto-connected), parsed, trimmed, and validated to look like
   * IAM principal ARNs. Never empty and never containing `*` — both are
   * rejected before this type is produced.
   */
  readonly allowedPrincipals: string[];
}

/**
 * A discriminated union, not one interface with optional fields: every
 * reader that needs `privateEntry` is only reachable after narrowing on
 * `mode === 'private'` or `mode === 'both'`, so there is no `privateEntry!`
 * non-null assertion anywhere this type is consumed.
 */
export type EntryConfig =
  | { readonly mode: 'cloudfront' }
  | { readonly mode: 'private'; readonly privateEntry: PrivateEntryConfig }
  | { readonly mode: 'both'; readonly privateEntry: PrivateEntryConfig };

/**
 * `true` whenever this entry mode has a CloudFront leg at all (`cloudfront`
 * and `both` — not `private`). Reviewed design note (Fable 5.2, both-mode
 * design review): `lib/network-stack.ts`'s SG wiring used to be a single
 * `if (entryMode === 'cloudfront') { ... } else { ... }`, which is exactly
 * the shape that silently drops a mode when a THIRD value is added — `both`
 * needed BOTH branches to run, and the original `if`/`else` could not say
 * that at all. The two independent `if` blocks that replaced it (and every
 * other place in `bin/iac.ts` that branches the same way) use this function
 * — and `hasPrivateLinkLeg` below — instead of a raw `=== 'cloudfront' ||
 * === 'both'` comparison, at every call site that does not ALSO need
 * TypeScript to narrow `EntryConfig` itself down to its `privateEntry`-
 * carrying members afterward (`bin/iac.ts`'s two sites that read
 * `entryConfig.privateEntry` inside the branch keep the raw literal
 * comparison on purpose: TypeScript's control-flow analysis is documented
 * to narrow a discriminated union from a direct comparison on its
 * discriminant, and this file does not depend on the less certain case of
 * that narrowing surviving a call through a plain boolean-returning
 * function).
 */
export function hasCloudFrontLeg(mode: EntryConfig['mode']): boolean {
  return mode === 'cloudfront' || mode === 'both';
}

/** `true` whenever this entry mode has a PrivateLink leg at all (`private` and `both` — not `cloudfront`). See `hasCloudFrontLeg`'s doc. */
export function hasPrivateLinkLeg(mode: EntryConfig['mode']): boolean {
  return mode === 'private' || mode === 'both';
}

export type Env = Record<string, string | undefined>;

// `aws` partition only, deliberately — same posture as region-config.ts's
// `assertRegion` (GovCloud / China have no CloudFront for the `cloudfront`
// mode, and PrivateLink's shape has not been verified against those
// partitions for this mode either; an `arn:aws-cn:...` or `arn:aws-us-gov:...`
// principal is refused here even though its shape is otherwise valid). The
// resource-name character class includes `/`, because IAM role and user
// names may themselves be path-qualified (e.g. `role/team/ops-role`); `root`
// never takes a resource name at all.
const IAM_PRINCIPAL_ARN_RE =
  /^arn:aws:iam::\d{12}:(root|(?:user|role)\/[\w+=,.@/-]+)$/;

// A bare lowercase hostname: no scheme, no path, no port, no uppercase, at
// least two dot-separated labels. The character class alone already
// refuses every one of those: a scheme or a path introduces `:` or `/`, a
// port introduces `:`, and uppercase letters are outside `[a-z0-9]`/
// `[a-z0-9-]` entirely — so one regex is the complete check, not a
// pre-filter for some cases plus this for the rest. Matches RFC 1123 label
// rules: each label starts and ends with an alphanumeric, with only
// alphanumerics and hyphens in between, and is at most 63 characters.
const BARE_HOSTNAME_RE =
  /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;

function parsePrincipalList(raw: string): string[] {
  return raw
    .split(',')
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}

/**
 * Parse and validate `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN` /
 * `STRATOCLAVE_PRIVATE_DOMAIN` / `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS`.
 *
 * Shared by `mode: 'private'` and `mode: 'both'` — both need an identical
 * PrivateLink leg (same three variables, same validation), so this is the
 * one place either throws, rather than two call sites that could drift.
 * `modeLabel` only changes the wording of the error messages (`"private"`
 * vs `"both"`), never the validation itself.
 */
function resolvePrivateEntryConfig(
  env: Env,
  bodyRegion: string,
  modeLabel: 'private' | 'both',
): PrivateEntryConfig {
  const certificateArn = (env.STRATOCLAVE_PRIVATE_CERTIFICATE_ARN ?? '').trim();
  const domainName = (env.STRATOCLAVE_PRIVATE_DOMAIN ?? '').trim();
  const allowedPrincipalsRaw = (env.STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS ?? '').trim();

  const missing: string[] = [];
  if (!certificateArn) missing.push('STRATOCLAVE_PRIVATE_CERTIFICATE_ARN');
  if (!domainName) missing.push('STRATOCLAVE_PRIVATE_DOMAIN');
  if (!allowedPrincipalsRaw) missing.push('STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS');
  if (missing.length > 0) {
    throw new Error(
      `STRATOCLAVE_ENTRY=${modeLabel} requires ${missing.join(', ')} to be set. The PrivateLink ` +
        'leg has no CloudFront and no web console of its own to serve a certificate or ' +
        'discover a domain from — the operator supplies both, plus the set of IAM principals ' +
        'allowed to request a connection to the endpoint service. See docs/DEPLOYMENT.md, ' +
        '"Private entry (PrivateLink), headless".',
    );
  }

  const certRe = new RegExp(`^arn:aws:acm:${bodyRegion}:\\d{12}:certificate/[0-9a-f-]+$`);
  if (!certRe.test(certificateArn)) {
    throw new Error(
      `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN ("${certificateArn}") must be an ACM ` +
        `certificate ARN in the deployment's own region (${bodyRegion}): ` +
        `arn:aws:acm:${bodyRegion}:<account>:certificate/<id>. An ALB cannot use a ` +
        "certificate from a different region — ACM certificates are regional, and " +
        'a cross-region ARN fails at deploy, not at synth, with a much less direct error.',
    );
  }

  if (!BARE_HOSTNAME_RE.test(domainName)) {
    throw new Error(
      `STRATOCLAVE_PRIVATE_DOMAIN ("${domainName}") must be a bare lowercase hostname — ` +
        'no scheme (no "https://"), no path, no port, no uppercase, at least two ' +
        'dot-separated DNS labels, e.g. "stratoclave.internal.example.com". It is ' +
        'interpolated into `https://<domain>` for both the ALB\'s certificate domain ' +
        'and the literal value of STRATOCLAVE_API_ENDPOINT.',
    );
  }

  const allowedPrincipals = parsePrincipalList(allowedPrincipalsRaw);
  if (allowedPrincipals.length === 0) {
    throw new Error(
      'STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS must list at least one IAM principal ARN ' +
        `(got "${allowedPrincipalsRaw}", which parsed to an empty list).`,
    );
  }
  const wildcarded = allowedPrincipals.filter((p) => p.includes('*'));
  if (wildcarded.length > 0) {
    throw new Error(
      'STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS must not contain a wildcard ' +
        `(found: ${wildcarded.join(', ')}). The whole point of acceptance-required ` +
        'PrivateLink is that the operator names which accounts, roles, or users may ' +
        'even ASK to connect; a wildcard anywhere in an entry — a bare "*", "root*", ' +
        '"role/*" — lets a connection request from anyone or anything matching it reach ' +
        'this endpoint service at all, which is the public-internet failure mode this ' +
        'mode exists to avoid.',
    );
  }
  const malformed = allowedPrincipals.filter((p) => !IAM_PRINCIPAL_ARN_RE.test(p));
  if (malformed.length > 0) {
    throw new Error(
      'STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS entries must be aws-partition IAM principal ' +
        'ARNs (arn:aws:iam::<12-digit-account>:root, :user/<name>, or :role/<name>). Not ' +
        `one: ${malformed.join(', ')}.`,
    );
  }

  return { certificateArn, domainName, allowedPrincipals };
}

/**
 * Resolve the effective entry mode and, for `private`/`both`, its required
 * PrivateLink configuration — or throw with an actionable message. Pure:
 * depends only on `env` and `bodyRegion` (the already-resolved deploy
 * region, from `resolveRegionConfig`, needed to check the certificate's own
 * region matches).
 */
export function resolveEntryConfig(env: Env, bodyRegion: string): EntryConfig {
  // `?? 'cloudfront'` alone would not catch an explicit empty string
  // (`STRATOCLAVE_ENTRY=""`), which nullish coalescing treats as "set" —
  // and an operator's deploy tooling stripping a variable down to empty
  // rather than removing it is a real shape, not a hypothetical one (see
  // `STRATOCLAVE_FAILOVER_REGIONS`'s own empty-string handling in
  // region-config.ts). Treat it the same as unset.
  const raw = (env.STRATOCLAVE_ENTRY || 'cloudfront').trim().toLowerCase();

  if (raw !== 'cloudfront' && raw !== 'private' && raw !== 'both') {
    throw new Error(
      `STRATOCLAVE_ENTRY must be "cloudfront", "private", or "both" (got "${env.STRATOCLAVE_ENTRY}").`,
    );
  }

  if (raw === 'cloudfront') {
    return { mode: 'cloudfront' };
  }

  const privateEntry = resolvePrivateEntryConfig(env, bodyRegion, raw);
  return raw === 'both'
    ? { mode: 'both', privateEntry }
    : { mode: 'private', privateEntry };
}

/**
 * `STRATOCLAVE_CLOUDFRONT_ORIGIN` — whether, and how far along, the
 * CloudFront-facing ALB has moved off a public address onto a CloudFront VPC
 * origin (`cloudfront_origins.VpcOrigin.withApplicationLoadBalancer`).
 *
 * This replaces a shared-secret header design (three review rounds, three new
 * outage paths, all "the two copies diverged" — see
 * docs/DEPLOYMENT.md's "Hardening the CloudFront to ALB hop" for the history)
 * with a network-reachability one: `vpc` mode's ALB has no public address at
 * all, so a third party's own CloudFront distribution cannot reach it
 * regardless of what it sends.
 *
 * Five values, not two, because an EXISTING `public` deployment cannot jump
 * straight to `vpc` without downtime (the ALB's internal/internet-facing
 * scheme cannot change in place; CloudFormation replaces it, and the
 * replacement has to be registered with CloudFront and healthy before the
 * old one can be removed, or every in-flight request through the old origin
 * fails for the outage window) — AND cannot go straight from `vpc-switch` to
 * `vpc` either, for a second, independent reason: see `vpc-contract`'s own
 * entry below. A FRESH deployment has neither constraint and may set `vpc`
 * directly — there is no old ALB to migrate away from, so "expand",
 * "switch" and "contract" all collapse to nothing being built at all for
 * the states they would otherwise have carried forward.
 *
 *   - `vpc` (default under `cloudfront`/`both` when unset or empty — see
 *     `defaultCloudfrontOrigin`'s doc for why `private`'s silent default is
 *     `public` instead, not `vpc`): the ALB CloudFront talks to has no
 *     public address at all. A fresh deployment gets exactly this from the
 *     start. An EXISTING `public` deployment is NOT silently moved onto
 *     this: see `iac/scripts/deploy-all.sh`'s pre-flight, which refuses a
 *     bare `cdk deploy --all` against a live internet-facing ALB unless
 *     `STRATOCLAVE_CLOUDFRONT_ORIGIN` is explicitly one of the migration
 *     states below, or explicitly `public`.
 *   - `public`: an explicit, documented fallback — one internet-facing ALB,
 *     its security group admitting the CloudFront origin-facing managed
 *     prefix list, CloudFront talking to it by DNS name over plain HTTP.
 *     The supported path for an account whose VPC sits in an AZ that VPC
 *     origins do not support (see docs/DEPLOYMENT.md's AZ-constraint
 *     section) and for a migration's starting state.
 *   - `vpc-expand`: adds a second, internal ALB (CDK-generated name) in new
 *     PRIVATE_ISOLATED subnets, its own target group, and the CloudFront VPC
 *     origin resource pointed at it — all alongside the untouched public ALB,
 *     its target group, and its CloudFront origin. The ECS service registers
 *     with BOTH target groups. Nothing about what CloudFront actually routes
 *     to changes in this step; its only job is to let the slow parts (VPC
 *     origin creation, the new ALB's target health) finish and be verified
 *     before any traffic depends on them. The public ALB's deletion
 *     protection is still whatever the deployment's normal setting is here
 *     (see `lib/alb-stack.ts`'s `publicAlbDeletionProtection` — nothing is
 *     being removed yet).
 *   - `vpc-switch`: the distribution's four ALB behaviours retarget from the
 *     public origin to the VPC origin. The old public ALB, its target group,
 *     and its (now unreferenced) CloudFront origin are all still declared —
 *     switching back is a one-line revert of the behaviours' `targetOriginId`
 *     while the old path is still warm, with no ALB to recreate first. From
 *     this state on, the public ALB's deletion protection is forced OFF
 *     (`lib/alb-stack.ts`) regardless of the deployment's normal setting:
 *     every state from here on is explicitly working towards deleting it.
 *   - `vpc-contract` (round-1 implementation review, blocker B1/the Sol
 *     review's matching blocker): a SEPARATE step from `vpc`, not a detail
 *     of it. `cdk deploy --all` always updates a producer stack before the
 *     consumer stacks that import its exports — that ordering is what makes
 *     normal deploys work, and it is exactly backwards for DELETING an
 *     export: going straight from `vpc-switch` to `vpc` would have
 *     `AlbStack` (the producer of the public ALB's DNS name and target
 *     group ARN) try to delete both exports in the SAME `cdk deploy --all`
 *     run where `FrontendStack`/`EcsStack` (the consumers) also stop
 *     importing them — and CDK's own stack ordering runs the producer's
 *     update FIRST, so CloudFormation refuses it outright ("Export ... cannot
 *     be deleted as it is in use by ..."), stalling with the public bypass
 *     still open. `vpc-contract` breaks the deadlock by moving the CONSUMER
 *     side first, on its own: `lib/frontend-stack.ts` stops declaring
 *     `ALBOrigin` and every behaviour's `targetOriginId` is already (still)
 *     `ALBVpcOrigin` from `vpc-switch`; `lib/ecs-stack.ts`'s backend service
 *     stops registering with the public target group (only the internal one
 *     remains in its `LoadBalancers` list) — while `lib/alb-stack.ts` still
 *     builds the public ALB, its target group, and its exports UNCHANGED
 *     from `vpc-switch`, so there is nothing for CloudFormation to refuse.
 *     Only once this step has deployed successfully (nothing importing the
 *     public exports anymore) does the FOLLOWING `vpc` deploy let AlbStack
 *     delete them — see `vpc`'s own entry below, and
 *     `iac/test/vpc-origin-migration.test.ts`'s cross-stack-export
 *     transition test, which checks every `N -> N+1` step (including this
 *     one) rather than only synthesising each state alone.
 *   - `vpc`: ALSO the contract step for a FRESH deployment (which never had
 *     a public ALB to remove exports from, so there is nothing for the
 *     deadlock above to apply to) — but for an EXISTING deployment migrating
 *     off `public`, it must be reached FROM `vpc-contract`, never directly
 *     from `vpc-switch` (see that entry). Removes the public ALB, its target
 *     group, its exports, and its CloudFront origin; only the internal ALB
 *     and the VPC origin remain. This is the step that actually closes the
 *     public bypass for an existing deployment — see docs/DEPLOYMENT.md's
 *     migration section, which names it plainly rather than leaving it
 *     implied by the state name.
 *
 * Rollback is the same five states read backwards, with the same ordering
 * constraint reversed: going back (`vpc` -> `vpc-contract` -> `vpc-switch` ->
 * `vpc-expand` -> `public`) re-ADDS a producer export at each step rather
 * than removing one, so the deadlock above does not apply to any single
 * rollback step on its own — EXCEPT the first one, `vpc` -> `vpc-contract`,
 * which recreates the public ALB. See docs/DEPLOYMENT.md's "Rollback"
 * section for the exact `deploy-all.sh` invocation per step; `cdk deploy
 * --all` (via `deploy-all.sh`, never bare) is the correct tool for every
 * rollback step because CDK, not this file, decides the per-stack order and
 * always deploys a producer before a consumer that newly depends on it.
 *
 * Refused outright, not degraded, when combined with `STRATOCLAVE_ENTRY=private`:
 * that path has no CloudFront distribution for a VPC origin to belong to, so
 * every value here would be silently inert on it. `STRATOCLAVE_ENTRY=both`
 * instead FORCES `vpc` (refusing every other value, including the unset
 * default's literal spelling `public`/`vpc-expand`/`vpc-switch`/
 * `vpc-contract`): a `both` deployment's whole point is no public ALB at
 * all, ever, so there is nothing for the migration states to migrate
 * between.
 */
export type CloudfrontOriginMode =
  | 'public'
  | 'vpc-expand'
  | 'vpc-switch'
  | 'vpc-contract'
  | 'vpc';

const CLOUDFRONT_ORIGIN_MODES: readonly CloudfrontOriginMode[] = [
  'public',
  'vpc-expand',
  'vpc-switch',
  'vpc-contract',
  'vpc',
];

/**
 * The SILENT default (unset/empty `STRATOCLAVE_CLOUDFRONT_ORIGIN`) for a
 * given entry mode — see `resolveCloudfrontOriginConfig`'s own comment for
 * why this cannot be a single constant.
 */
function defaultCloudfrontOrigin(entryMode: EntryConfig['mode']): string {
  // `private` has no CloudFront distribution at all; "public" is the only
  // value that is a true no-op for it (every other value is refused below
  // regardless of whether it got there by default or was set explicitly —
  // this function's job is only to pick what "unset" quietly means).
  return entryMode === 'private' ? 'public' : 'vpc';
}

/**
 * Resolve `STRATOCLAVE_CLOUDFRONT_ORIGIN`. Pure, same convention as
 * `resolveEntryConfig` above: trim, lowercase, empty-or-unset is the
 * default, anything else not in the closed set throws.
 *
 * `entryMode` is the already-resolved `EntryConfig['mode']` (not re-read from
 * `env`), so this function and `resolveEntryConfig` can never disagree about
 * which entry mode is in effect.
 */
export function resolveCloudfrontOriginConfig(
  env: Env,
  entryMode: EntryConfig['mode'],
): CloudfrontOriginMode {
  // Unset (or empty) is NOT always "vpc": for `private`, where this
  // variable has no effect at all, defaulting it to anything other than
  // "public" would make every pre-existing `STRATOCLAVE_ENTRY=private`
  // deployment — which never set this variable, because it did not exist
  // yet — fail at synth the moment this default flipped to "vpc". The
  // default is resolved per `entryMode` BEFORE validation/combo-checking
  // runs; an EXPLICIT value (anything the operator actually set) is still
  // validated and combo-checked exactly as written, so
  // `STRATOCLAVE_ENTRY=private STRATOCLAVE_CLOUDFRONT_ORIGIN=vpc` is still
  // refused below — only the SILENT default is mode-aware, not the
  // explicit-value handling.
  const rawInput = (env.STRATOCLAVE_CLOUDFRONT_ORIGIN ?? '').trim();
  const raw = rawInput === '' ? defaultCloudfrontOrigin(entryMode) : rawInput.toLowerCase();

  if (!CLOUDFRONT_ORIGIN_MODES.includes(raw as CloudfrontOriginMode)) {
    throw new Error(
      `STRATOCLAVE_CLOUDFRONT_ORIGIN must be one of ${CLOUDFRONT_ORIGIN_MODES.join(', ')} ` +
        `(got "${env.STRATOCLAVE_CLOUDFRONT_ORIGIN}").`,
    );
  }
  const mode = raw as CloudfrontOriginMode;

  if (mode !== 'public' && entryMode === 'private') {
    throw new Error(
      `STRATOCLAVE_CLOUDFRONT_ORIGIN=${mode} has no effect under STRATOCLAVE_ENTRY=private: ` +
        'that path has no CloudFront distribution for a VPC origin to belong to. Unset ' +
        'STRATOCLAVE_CLOUDFRONT_ORIGIN (or set it to "public") when deploying STRATOCLAVE_ENTRY=private.',
    );
  }

  if (entryMode === 'both' && mode !== 'vpc') {
    throw new Error(
      `STRATOCLAVE_CLOUDFRONT_ORIGIN=${mode} is refused under STRATOCLAVE_ENTRY=both: a "both" ` +
        'deployment keeps exactly one ALB, internal only, serving both CloudFront (via a VPC ' +
        'origin) and PrivateLink — there is no public ALB for "public"/"vpc-expand"/"vpc-switch" ' +
        'to describe a state of. Unset STRATOCLAVE_CLOUDFRONT_ORIGIN (or set it to "vpc") when ' +
        'deploying STRATOCLAVE_ENTRY=both.',
    );
  }

  return mode;
}
