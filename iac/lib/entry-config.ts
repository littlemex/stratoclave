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
 *   - `cloudfront` (default, and also when unset or empty): today's stack
 *     set, byte-for-byte. CloudFront + S3 console in front of an
 *     internet-facing ALB.
 *   - `private`: no CloudFront, no S3 console, no CLOUDFRONT-scope WAF. An
 *     internal ALB reached only through AWS PrivateLink (an internal NLB plus
 *     a VPC endpoint service). Requires an operator-supplied ACM certificate
 *     (in the same region as the deployment), a bare domain name, and a
 *     non-empty, non-wildcard allowed-principals list — all three are
 *     refused at synth, loudly, when missing or malformed, because an
 *     internal entry point with no certificate or no allowed principal is
 *     not a smaller version of the feature, it is a different and broken
 *     one.
 *   - `both`: refused at synth for now. It would add reach without
 *     restricting anything — a private team's leaked key would still work
 *     from the internet — and it needs a second internal ALB plus a way to
 *     decide which entry a request arrived on that this slice does not
 *     build. Tracked as a follow-up, not shipped here.
 */

export interface PrivateEntryConfig {
  /** `STRATOCLAVE_PRIVATE_CERTIFICATE_ARN` — an ACM certificate for the ALB's HTTPS listener, in the deployment's own region. */
  readonly certificateArn: string;
  /** `STRATOCLAVE_PRIVATE_DOMAIN` — a bare hostname, e.g. `stratoclave.internal.example.com`. */
  readonly domainName: string;
  /**
   * `STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS` — IAM principal ARNs allowed to
   * accept a VPC endpoint connection to the endpoint service, parsed,
   * trimmed, and validated to look like IAM principal ARNs. Never empty and
   * never containing `*` — both are rejected before this type is produced.
   */
  readonly allowedPrincipals: string[];
}

/**
 * A discriminated union, not one interface with an optional field: every
 * reader that needs `privateEntry` is only reachable after narrowing on
 * `mode === 'private'`, so there is no `privateEntry!` non-null assertion
 * anywhere this type is consumed.
 */
export type EntryConfig =
  | { readonly mode: 'cloudfront' }
  | { readonly mode: 'private'; readonly privateEntry: PrivateEntryConfig };

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

// A bare lowercase hostname: no scheme, no path, no port, at least two
// labels (so a single word is refused, which already catches a copy-pasted
// `https://...` or a trailing slash via the dot requirement combined with
// the scheme/slash checks below). Matches RFC 1123 label rules: each label
// starts and ends with an alphanumeric and is <= 63 characters internally
// hyphen-only.
const BARE_HOSTNAME_RE =
  /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;

function parsePrincipalList(raw: string): string[] {
  return raw
    .split(',')
    .map((p) => p.trim())
    .filter((p) => p.length > 0);
}

/**
 * Resolve the effective entry mode and, for `private`, its required
 * configuration — or throw with an actionable message. Pure: depends only on
 * `env` and `bodyRegion` (the already-resolved deploy region, from
 * `resolveRegionConfig`, needed to check the certificate's own region
 * matches).
 */
export function resolveEntryConfig(env: Env, bodyRegion: string): EntryConfig {
  // `?? 'cloudfront'` alone would not catch an explicit empty string
  // (`STRATOCLAVE_ENTRY=""`), which nullish coalescing treats as "set" —
  // and an operator's deploy tooling stripping a variable down to empty
  // rather than removing it is a real shape, not a hypothetical one (see
  // `STRATOCLAVE_FAILOVER_REGIONS`'s own empty-string handling in
  // region-config.ts). Treat it the same as unset.
  const raw = (env.STRATOCLAVE_ENTRY || 'cloudfront').trim().toLowerCase();

  if (raw === 'both') {
    throw new Error(
      'STRATOCLAVE_ENTRY=both is not supported yet. As specified it would add reach ' +
        'without restricting anything: a team meant to be private would still have a ' +
        'key that works from the internet through CloudFront, because today\'s ALB ' +
        'security group admits the CloudFront origin-facing prefix list unconditionally. ' +
        'It also needs a second internal ALB and a way to tell which entry a request ' +
        'arrived on, neither of which this slice builds. Deploy "cloudfront" and ' +
        '"private" as two separate, fresh deployments instead.',
    );
  }

  if (raw !== 'cloudfront' && raw !== 'private') {
    throw new Error(
      `STRATOCLAVE_ENTRY must be "cloudfront" or "private" (got "${env.STRATOCLAVE_ENTRY}"). ` +
        '"both" is named but not supported yet; see the error it raises for why.',
    );
  }

  if (raw === 'cloudfront') {
    return { mode: 'cloudfront' };
  }

  // raw === 'private'
  const certificateArn = (env.STRATOCLAVE_PRIVATE_CERTIFICATE_ARN ?? '').trim();
  const domainName = (env.STRATOCLAVE_PRIVATE_DOMAIN ?? '').trim();
  const allowedPrincipalsRaw = (env.STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS ?? '').trim();

  const missing: string[] = [];
  if (!certificateArn) missing.push('STRATOCLAVE_PRIVATE_CERTIFICATE_ARN');
  if (!domainName) missing.push('STRATOCLAVE_PRIVATE_DOMAIN');
  if (!allowedPrincipalsRaw) missing.push('STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS');
  if (missing.length > 0) {
    throw new Error(
      `STRATOCLAVE_ENTRY=private requires ${missing.join(', ')} to be set. The private ` +
        'path has no CloudFront and no web console, so there is nothing to serve a ' +
        'certificate or discover a domain from — the operator supplies both, plus the ' +
        'set of IAM principals allowed to accept a connection to the endpoint service. ' +
        'See docs/DEPLOYMENT.md, "Private entry (PrivateLink), headless".',
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

  if (domainName.includes('://') || domainName.includes('/') || domainName !== domainName.toLowerCase()) {
    throw new Error(
      `STRATOCLAVE_PRIVATE_DOMAIN ("${domainName}") must be a bare lowercase hostname — ` +
        'no scheme (no "https://"), no path, no port, e.g. ' +
        '"stratoclave.internal.example.com". It is used both as the ALB\'s certificate ' +
        'domain and as the literal value of STRATOCLAVE_API_ENDPOINT, which the backend ' +
        '(main.py\'s CORS_ORIGINS validation) and the CLI both expect to be a bare host, ' +
        'not a URL.',
    );
  }
  if (!BARE_HOSTNAME_RE.test(domainName)) {
    throw new Error(
      `STRATOCLAVE_PRIVATE_DOMAIN ("${domainName}") does not look like a valid hostname ` +
        '(expected one or more dot-separated DNS labels, lowercase letters/digits/hyphens, ' +
        'e.g. "stratoclave.internal.example.com").',
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
        'even ask to connect; a wildcard anywhere in an entry — a bare "*", "root*", ' +
        '"role/*" — makes the endpoint service accept a connection request from anyone ' +
        'or anything matching it, which is the public-internet failure mode this mode ' +
        'exists to avoid.',
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

  return {
    mode: 'private',
    privateEntry: { certificateArn, domainName, allowedPrincipals },
  };
}
