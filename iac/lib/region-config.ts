/**
 * Pure region / residency resolution for the Stratoclave app entrypoint.
 *
 * Extracted from bin/iac.ts so it can be unit-tested IN-PROCESS (no `cdk synth`
 * subprocess). bin/iac.ts calls resolveRegionConfig(process.env) once and wires
 * the result into the stacks; tests call it with a plain env object.
 *
 * Design decisions encoded here (see the region-decoupling PR + Fable reviews):
 *  - Only WAF is pinned to us-east-1 (CLOUDFRONT-scope WebACL). The body region
 *    R is operator-chosen (STRATOCLAVE_REGION).
 *  - The Bedrock model primary region is independent of R and must be explicit
 *    when R != us-east-1 (never silently fall back to AWS_REGION).
 *  - Residency: default failover is filtered to the primary's jurisdiction so a
 *    non-US primary never back-doors into another jurisdiction; an explicit
 *    STRATOCLAVE_FAILOVER_REGIONS list is honoured verbatim. Geo inference
 *    profiles cannot certify single-region residency. STRATOCLAVE_RESIDENCY=
 *    strict turns leaks into synth errors.
 */

export const DEFAULT_REGION = 'us-east-1'; // historical single-region default (body)
export const WAF_REGION = 'us-east-1'; // AWS hard requirement: CLOUDFRONT-scope WebACL

// Default cross-region failover targets when STRATOCLAVE_FAILOVER_REGIONS is
// unset. Mirrors mvp/routing/chains.py::_DEFAULT_FAILOVER_REGIONS — if you
// change this, update BOTH (a py<->ts drift test,
// test_default_failover_regions_match_iac_constant, guards it). Filtered to the
// primary's jurisdiction below (residency safety).
const DEFAULT_FAILOVER_REGIONS = ['us-west-2', 'eu-west-1'];
const FAILOVER_DISABLE_SENTINELS = new Set(['', 'none', 'disabled', 'off']);
// Hardcoded in mvp/models.py — the codex path calls bedrock-mantle in these
// regions regardless of OPENAI_BEDROCK_REGIONS. A backend drift test
// (test_openai_region_residency_contract.py) guards that these stay in sync.
const OPENAI_REGISTRY_REGIONS = ['us-west-2', 'us-east-2'];
// Geo (cross-region) inference-profile prefixes. Denylist — extend as AWS ships
// new geographies. us-gov is included though the gov partition is rejected.
const GEO_PROFILE_RE = /^(us|eu|apac|global|us-gov)\./;

export interface RegionConfig {
  bodyRegion: string;
  wafRegion: string;
  bedrockPrimaryRegion: string;
  defaultBedrockModel: string;
  /** Failover regions to pass to the backend, or undefined to leave unset. */
  failoverRegionsEnv: string | undefined;
  codexEnabled: boolean;
  /** Residency warnings to surface as CDK Annotations (empty in the common case). */
  residencyWarnings: string[];
}

export type Env = Record<string, string | undefined>;

// Reject non-region strings AND unsupported partitions. CloudFront (hence the
// WAF stack) does not exist in the GovCloud / China partitions, and a single
// app/credential set cannot span partitions. (Fable review M-2)
export function assertRegion(label: string, value: string): void {
  if (!/^[a-z]{2}(-[a-z]+)+-\d$/.test(value)) {
    throw new Error(
      `Invalid ${label} "${value}" (expected an AWS region id like "us-east-1" / "eu-west-1").`
    );
  }
  if (value.startsWith('us-gov-') || value.startsWith('cn-')) {
    throw new Error(
      `${label} "${value}": Stratoclave supports the "aws" partition only ` +
        `(GovCloud / China partitions have no CloudFront for the WAF stack).`
    );
  }
}

function jurisdiction(region: string): string {
  return region.split('-')[0];
}

function parseRegionList(raw: string): string[] {
  return raw
    .split(',')
    .map((r) => r.trim())
    .filter((r) => r.length > 0);
}

/**
 * Resolve the effective failover region set from the env, applying the same
 * rules as mvp/routing/chains.py::failover_regions: unset -> built-in defaults
 * filtered to the primary's jurisdiction; sentinel/empty -> []; explicit list
 * -> verbatim. The primary is always stripped (it is target 0).
 */
export function effectiveFailoverRegions(env: Env, primaryRegion: string): string[] {
  const raw = env.STRATOCLAVE_FAILOVER_REGIONS;
  let candidates: string[];
  if (raw === undefined) {
    const primaryJuris = jurisdiction(primaryRegion);
    candidates = DEFAULT_FAILOVER_REGIONS.filter(
      (r) => jurisdiction(r) === primaryJuris
    );
  } else if (FAILOVER_DISABLE_SENTINELS.has(raw.trim().toLowerCase())) {
    candidates = [];
  } else {
    candidates = parseRegionList(raw);
  }
  const seen = new Set<string>([primaryRegion]);
  const out: string[] = [];
  for (const r of candidates) {
    if (!seen.has(r)) {
      seen.add(r);
      out.push(r);
    }
  }
  return out;
}

/**
 * Resolve the full region/residency configuration, or throw with an actionable
 * message. Pure: depends only on `env`.
 */
export function resolveRegionConfig(env: Env): RegionConfig {
  // Body region R. STRATOCLAVE_REGION wins; else the CDK ambient region; else
  // us-east-1 (preserves the historical single-region default -> zero diff).
  const bodyRegion =
    env.STRATOCLAVE_REGION || env.CDK_DEFAULT_REGION || DEFAULT_REGION;
  assertRegion('deploy region (STRATOCLAVE_REGION / CDK_DEFAULT_REGION)', bodyRegion);

  // Bedrock model primary region — independent of the deploy region. When body
  // != us-east-1 the operator MUST declare it; a silent fall back to AWS_REGION
  // would call Bedrock in a region that may not host the model.
  const bedrockPrimaryRegion =
    env.BEDROCK_PRIMARY_REGION ||
    (bodyRegion === DEFAULT_REGION ? DEFAULT_REGION : undefined);
  if (!bedrockPrimaryRegion) {
    throw new Error(
      `BEDROCK_PRIMARY_REGION must be set explicitly when the deploy region ` +
        `(${bodyRegion}) != us-east-1. Refusing to guess the Bedrock model region. ` +
        `NOTE: this is also required for \`cdk bootstrap\` (bootstrap synthesizes ` +
        `this app); set BEDROCK_PRIMARY_REGION=<model-region> before bootstrapping, ` +
        `or bootstrap without synth via \`cdk bootstrap --app "" aws://<acct>/${bodyRegion}\`.`
    );
  }
  assertRegion('BEDROCK_PRIMARY_REGION', bedrockPrimaryRegion);

  const defaultBedrockModel =
    env.DEFAULT_BEDROCK_MODEL || 'us.anthropic.claude-opus-4-7';

  const failoverRegionsEnv = env.STRATOCLAVE_FAILOVER_REGIONS;
  // Match the backend for every EXPLICIT value: mvp/openai_responses.py treats
  // codex as enabled iff `CODEX_ENABLED.lower() == "true"`. Using `!== 'false'`
  // here would flip an existing `CODEX_ENABLED=0`/`no`/`off` deployment to
  // enabled on the next synth — silently re-enabling codex (and, off us-east-1,
  // leaking prompts to the US registry regions). One deliberate divergence:
  // empty string is falsy in JS `||`, so `''` takes the IaC default ('true')
  // rather than the backend's bare-getenv 'false' — harmless because CDK always
  // injects the normalized String(codexEnabled) into the task, so the container
  // and this analysis always agree. Operators disable codex with 'false', not
  // ''. (Fable final review B-1)
  const codexEnabled = (env.CODEX_ENABLED || 'true').toLowerCase() === 'true';

  const effectiveFailover = effectiveFailoverRegions(env, bedrockPrimaryRegion);
  for (const r of effectiveFailover) {
    assertRegion('STRATOCLAVE_FAILOVER_REGIONS entry', r);
  }

  // Every region a PROMPT can actually reach at runtime, tagged with its source.
  const bedrockCallRegions: { region: string; source: string }[] = [
    { region: bedrockPrimaryRegion, source: 'model' },
    ...effectiveFailover.map((r) => ({ region: r, source: 'failover' })),
    ...(codexEnabled
      ? OPENAI_REGISTRY_REGIONS.map((r) => ({ region: r, source: 'codex' }))
      : []),
  ];
  // STRICT single-region residency: any region != the deploy region is a leak.
  const residencyLeaks = Array.from(
    new Set(
      bedrockCallRegions
        .filter((c) => c.region !== bodyRegion)
        .map((c) => `${c.region}(${c.source})`)
    )
  );

  const residencyRaw = (env.STRATOCLAVE_RESIDENCY || '').toLowerCase();
  if (residencyRaw && residencyRaw !== 'strict' && residencyRaw !== 'warn') {
    throw new Error(
      `STRATOCLAVE_RESIDENCY must be "strict" or "warn" (got "${env.STRATOCLAVE_RESIDENCY}").`
    );
  }
  const residencyStrict = residencyRaw === 'strict';

  // The leak analysis only runs under residency intent — a non-default body
  // region or an explicit STRATOCLAVE_RESIDENCY — so a plain us-east-1 deploy
  // stays silent (backward compatible).
  const residencyIntent =
    bodyRegion !== DEFAULT_REGION || env.STRATOCLAVE_RESIDENCY !== undefined;

  const modelIsGeoProfile = GEO_PROFILE_RE.test(defaultBedrockModel);
  const allowGeoInference =
    (env.STRATOCLAVE_ALLOW_GEO_INFERENCE || '').toLowerCase() === 'true';

  const residencyWarnings: string[] = [];
  // Model-region != deploy-region is always noteworthy (prompt bytes leave R).
  if (bedrockPrimaryRegion !== bodyRegion) {
    residencyWarnings.push(
      `[residency] prompt data leaves the deploy region ${bodyRegion}: ` +
        `Bedrock primary = ${bedrockPrimaryRegion}.`
    );
  }
  // Geo-profile residency check: only meaningful under residency intent.
  if (residencyIntent && modelIsGeoProfile) {
    const geoMsg =
      `[residency] DEFAULT_BEDROCK_MODEL="${defaultBedrockModel}" is a geo cross-region ` +
      `inference profile — AWS routes inference anywhere within its geography, so a ` +
      `single-region residency guarantee for the deploy region ${bodyRegion} cannot ` +
      `be made. Use a directly-hosted (region-specific, non-"us./eu./apac./global."-` +
      `prefixed) model id, or set STRATOCLAVE_ALLOW_GEO_INFERENCE=true to accept ` +
      `geography-level (not region-level) residency.`;
    if (residencyStrict && !allowGeoInference) {
      throw new Error(
        `STRATOCLAVE_RESIDENCY=strict: refusing to synth — model is a geo inference ` +
          `profile.\n${geoMsg}`
      );
    }
    residencyWarnings.push(geoMsg);
  }
  if (residencyIntent && residencyLeaks.length > 0) {
    const hints: string[] = [];
    if (effectiveFailover.some((r) => r !== bodyRegion)) {
      hints.push('set STRATOCLAVE_FAILOVER_REGIONS=disabled (or to same-region only)');
    }
    if (codexEnabled && OPENAI_REGISTRY_REGIONS.some((r) => r !== bodyRegion)) {
      hints.push(
        `set CODEX_ENABLED=false (the OpenAI/codex path is hardwired to ` +
          `${OPENAI_REGISTRY_REGIONS.join(', ')} in the model registry and cannot be relocated)`
      );
    }
    if (bedrockPrimaryRegion !== bodyRegion) {
      hints.push(`set BEDROCK_PRIMARY_REGION=${bodyRegion}`);
    }
    const msg =
      `[residency] prompts can reach region(s) other than the deploy region ` +
      `${bodyRegion}: ${residencyLeaks.join(', ')}. ` +
      `For strict single-region residency: ${hints.join('; ')}.`;
    if (residencyStrict) {
      throw new Error(
        `STRATOCLAVE_RESIDENCY=strict: refusing to synth — Bedrock is reachable ` +
          `outside the deploy region.\n${msg}`
      );
    }
    residencyWarnings.push(msg);
  }

  return {
    bodyRegion,
    wafRegion: WAF_REGION,
    bedrockPrimaryRegion,
    defaultBedrockModel,
    failoverRegionsEnv,
    codexEnabled,
    residencyWarnings,
  };
}
