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
 *  - Inference boundary: STRATOCLAVE_INFERENCE_REGIONS is a hard allowlist of
 *    regions Bedrock inference may run in, AWS-enforced (not just checked at
 *    synth) via the IAM this module's output drives in lib/ecs-stack.ts and the
 *    container env the backend reads. A different mechanism from
 *    STRATOCLAVE_RESIDENCY, so the two cannot both be set. See
 *    docs/DEPLOYMENT.md, "Keeping inference inside a set of regions".
 */

import * as fs from 'fs';
import * as path from 'path';

export const DEFAULT_REGION = 'us-east-1'; // historical single-region default (body)
export const WAF_REGION = 'us-east-1'; // AWS hard requirement: CLOUDFRONT-scope WebACL
// The fallback DEFAULT_BEDROCK_MODEL when the operator sets none. Named so
// every message that talks about it interpolates this constant rather than
// repeating the literal, which would drift silently if this value ever
// changes.
export const DEFAULT_BEDROCK_MODEL_FALLBACK = 'us.anthropic.claude-opus-5';

// Default cross-region failover targets when STRATOCLAVE_FAILOVER_REGIONS is
// unset. Mirrors mvp/routing/chains.py::_DEFAULT_FAILOVER_REGIONS — if you
// change this, update BOTH (a py<->ts drift test,
// test_default_failover_regions_match_iac_constant, guards it). Filtered to the
// primary's jurisdiction below (residency safety).
const DEFAULT_FAILOVER_REGIONS = ['us-west-2', 'eu-west-1'];
const FAILOVER_DISABLE_SENTINELS = new Set(['', 'none', 'disabled', 'off']);
// Geo (cross-region) inference-profile prefixes. Denylist — extend as AWS ships
// new geographies. us-gov is included though the gov partition is rejected.
// `jp`/`au`/`ca` mirror the backend's own list
// (`backend/mvp/pricing_feeds/dimensions.py::_GEO_PROFILE_PREFIXES`); keep the
// two in sync when AWS ships another geography.
const GEO_PROFILE_RE = /^(us|eu|apac|jp|au|ca|global|us-gov)\./;

/** The shape this module reads out of backend/mvp/defaults/models.json. Only
 * the fields used below are declared; the real file carries more. */
interface ModelRegistryEntry {
  bedrock_model_id: string;
  bedrock_region: string;
  wire_protocol: string;
  [key: string]: unknown;
}

function defaultModelRegistryPath(): string {
  return path.resolve(__dirname, '..', '..', 'backend', 'mvp', 'defaults', 'models.json');
}

/**
 * Load the bundled Bedrock model registry — the SAME file
 * `backend/mvp/models.py` loads its `_REGISTRY` from (`_REGISTRY_FILENAME =
 * "defaults/models.json"`) — so the checks below read one source of truth for
 * which Bedrock region an entry actually calls, instead of a hand-maintained
 * TS table that can drift from it. `STRATOCLAVE_MODEL_REGISTRY_PATH`
 * overrides the path (e.g. for a test fixture); unset reads the real file,
 * resolved relative to this module so it does not depend on process cwd.
 */
function loadModelRegistry(env: Env): ModelRegistryEntry[] {
  const registryPath = env.STRATOCLAVE_MODEL_REGISTRY_PATH || defaultModelRegistryPath();
  let raw: string;
  try {
    raw = fs.readFileSync(registryPath, 'utf-8');
  } catch (e) {
    // `new Error(message, { cause })` needs an ES2022 lib target this app
    // does not build with; Object.assign attaches a real `cause` property
    // (readable by any ES2022-aware tooling) without needing it.
    throw Object.assign(
      new Error(
        `Could not read the Bedrock model registry at "${registryPath}" (override with ` +
          `STRATOCLAVE_MODEL_REGISTRY_PATH; default is backend/mvp/defaults/models.json): ` +
          `${(e as Error).message}`
      ),
      { cause: e }
    );
  }
  let doc: { models?: ModelRegistryEntry[] };
  try {
    doc = JSON.parse(raw) as { models?: ModelRegistryEntry[] };
  } catch (e) {
    throw Object.assign(
      new Error(
        `Bedrock model registry at "${registryPath}" is not valid JSON: ${(e as Error).message}`
      ),
      { cause: e }
    );
  }
  return doc.models ?? [];
}

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
  /** Deprecated-env-var-name warnings to surface as CDK Annotations (empty in the common case). */
  deprecationWarnings: string[];
  /**
   * Parsed STRATOCLAVE_INFERENCE_REGIONS — a hard allowlist of regions Bedrock
   * inference may run in — or `undefined` when unset (no boundary, today's
   * behaviour). Unlike `residencyWarnings` (synth-time warn/refuse only), this
   * list also drives IAM generation in lib/ecs-stack.ts and is forwarded to the
   * backend container, so AWS itself — not just this check — refuses anything
   * outside it. See docs/DEPLOYMENT.md, "Keeping inference inside a set of
   * regions".
   */
  inferenceRegions: string[] | undefined;
  /**
   * Informational CDK Annotation naming the effective boundary ("none" when
   * unset), always present (length-1 message), so a redeploy from a shell
   * without STRATOCLAVE_INFERENCE_REGIONS set is visible in `cdk synth`/`cdk diff`
   * rather than silently widening back to "no boundary".
   */
  inferenceBoundaryInfo: string;
  /**
   * Warnings specific to the inference-region boundary (failover ignored under
   * it / the deploy region itself sits outside it). Surfaced as CDK Annotations,
   * same mechanism as `residencyWarnings`. Empty when no boundary is set.
   */
  inferenceBoundaryWarnings: string[];
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
 * Parse STRATOCLAVE_INFERENCE_REGIONS: comma-separated, each entry trimmed and
 * validated with `assertRegion`. Unlike `parseRegionList` (used for the
 * failover list, which silently drops blank items), a boundary is a security
 * allowlist — a blank item, an empty overall list, or a duplicate is a
 * misconfiguration and MUST throw rather than silently narrowing to something
 * the operator did not ask for.
 */
function parseInferenceRegionsList(raw: string): string[] {
  // `"".split(',')` is `['']` (length 1, not 0) and a comma-only string is
  // likewise never empty, so every blank-overall-list case this function
  // needs to refuse is already a blank ITEM — there is no separate
  // zero-length case to check.
  const items = raw.split(',').map((r) => r.trim());
  if (items.some((r) => r.length === 0)) {
    throw new Error(
      `STRATOCLAVE_INFERENCE_REGIONS must be a non-empty comma-separated list of ` +
        `AWS region ids with no blank entries (got "${raw}"). Example: ` +
        `STRATOCLAVE_INFERENCE_REGIONS=ap-northeast-1,ap-northeast-3`
    );
  }
  const seen = new Set<string>();
  for (const r of items) {
    if (seen.has(r)) {
      throw new Error(
        `STRATOCLAVE_INFERENCE_REGIONS lists "${r}" more than once (got "${raw}").`
      );
    }
    seen.add(r);
  }
  for (const r of items) {
    assertRegion('STRATOCLAVE_INFERENCE_REGIONS entry', r);
  }
  return items;
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
    env.DEFAULT_BEDROCK_MODEL || DEFAULT_BEDROCK_MODEL_FALLBACK;

  // Match the backend for every EXPLICIT value: mvp/openai_responses.py treats
  // codex as enabled iff `STRATOCLAVE_CODEX_ENABLED.lower() == "true"`. Using
  // `!== 'false'` here would flip an existing `..._ENABLED=0`/`no`/`off`
  // deployment to enabled on the next synth — silently re-enabling codex
  // (and, off us-east-1, leaking prompts to the US registry regions).
  //
  // Default is TRUE. An earlier revision of this comment defaulted it to
  // FALSE on the theory that every flag gating money/route exposure should
  // ship conservative (STRATOCLAVE_HARD_CEILING_GATE, STRATOCLAVE_UNOBSERVED_HOLDS,
  // STRATOCLAVE_RESIDENCY, STRATOCLAVE_RESERVE_PROTOCOL do). Codex is not one
  // of those: it does not, by itself, spend a tenant's money or relax a
  // safety check — every request through it still goes through the SAME
  // reservation/settlement pipeline as the Anthropic route, gated by the
  // SAME pool/quota walls. What a default-off codex actually gates is
  // whether `stratoclave codex` *works at all* for an operator who deployed
  // this gateway and never separately discovered and set an extra env var —
  // and since the route's own refusal for that case is a bare HTTP 503 with
  // no operator-facing signal at deploy time, the failure mode is silent
  // until someone tries it — an operator who follows the docs and deploys
  // gets a working `stratoclave claude` and a `stratoclave codex` that 503s
  // for a reason nothing in the deploy output mentioned. Codex is one of
  // this gateway's two supported CLIs, not an optional add-on, so it now
  // ships the way the Anthropic route always has: on by default,
  // with STRATOCLAVE_CODEX_ENABLED=false as the explicit opt-out for an
  // operator who wants the surface gone (e.g. residency: the codex path is
  // registry-pinned to us-east-2/us-west-2 and cannot be relocated).
  //
  // `CODEX_ENABLED` (bare, unnamespaced) is the deprecated predecessor name,
  // honoured as a fallback only when `STRATOCLAVE_CODEX_ENABLED` is unset, so
  // an existing deployment does not silently disable codex on the next synth
  // just because it never renamed its env var. Presence (`!== undefined`) is
  // checked rather than JS truthiness so an explicit `''` on either name is
  // treated as "set, evaluates to disabled" — the same as the backend's
  // bare-getenv reading of an explicit empty string.
  const deprecationWarnings: string[] = [];
  let codexEnabledRaw: string;
  if (env.STRATOCLAVE_CODEX_ENABLED !== undefined) {
    codexEnabledRaw = env.STRATOCLAVE_CODEX_ENABLED;
  } else if (env.CODEX_ENABLED !== undefined) {
    codexEnabledRaw = env.CODEX_ENABLED;
    deprecationWarnings.push(
      'CODEX_ENABLED is deprecated; set STRATOCLAVE_CODEX_ENABLED instead. ' +
        'The bare name is still honoured for this synth but support for it ' +
        'will be removed in a future release.',
    );
  } else {
    codexEnabledRaw = 'true';
  }
  const codexEnabled = codexEnabledRaw.toLowerCase() === 'true';

  // The bundled Bedrock model registry, read once per call (see
  // loadModelRegistry's comment for why this is the file backend/mvp/models.py
  // itself loads, not a second copy of it). `openaiRegistryRegions` is the
  // set of Bedrock regions the codex/responses-protocol entries actually
  // call from — this REPLACES a hand-maintained constant that used to list
  // the same two regions by hand; backend/tests/test_openai_region_residency_contract.py
  // independently pins its own expectation of this set against the SAME
  // file (via mvp.models._REGISTRY), so the two stay in sync by both reading
  // one source rather than by a human updating two.
  const modelRegistry = loadModelRegistry(env);
  const openaiRegistryRegions = Array.from(
    new Set(
      modelRegistry
        .filter((m) => m.wire_protocol === 'responses')
        .map((m) => m.bedrock_region)
    )
  );

  // --- Inference-region boundary (STRATOCLAVE_INFERENCE_REGIONS) -----------
  // A hard allowlist of AWS regions Bedrock inference may run in. Unset means
  // no boundary (today's behaviour). This is a DIFFERENT mechanism from
  // STRATOCLAVE_RESIDENCY: residency only warns or hard-fails THIS synth on a
  // leak the app would reach; the boundary also drives what the ECS task
  // role's IAM policy actually grants (lib/ecs-stack.ts) and what the backend
  // container is told to serve, so AWS itself refuses anything outside it —
  // not merely this check. The two mechanisms address the same concern
  // (where Bedrock inference may run) through different enforcement points,
  // so setting both is rejected rather than silently picking one.
  const inferenceRegionsRaw = env.STRATOCLAVE_INFERENCE_REGIONS;
  const inferenceRegions: string[] | undefined =
    inferenceRegionsRaw === undefined
      ? undefined
      : parseInferenceRegionsList(inferenceRegionsRaw);

  if (inferenceRegions !== undefined && env.STRATOCLAVE_RESIDENCY !== undefined) {
    throw new Error(
      `STRATOCLAVE_INFERENCE_REGIONS and STRATOCLAVE_RESIDENCY cannot both be set: ` +
        `they are two different mechanisms for the same concern (where Bedrock ` +
        `inference may run), and having both set leaves it ambiguous which one ` +
        `governs. Keep STRATOCLAVE_INFERENCE_REGIONS — it is enforced by AWS IAM, not ` +
        `just a synth-time warning or refusal — and unset STRATOCLAVE_RESIDENCY; or, ` +
        `if you specifically want the single-region/geo-profile synth-time checks ` +
        `instead, unset STRATOCLAVE_INFERENCE_REGIONS.`
    );
  }

  const inferenceBoundaryWarnings: string[] = [];
  if (inferenceRegions !== undefined) {
    if (!inferenceRegions.includes(bedrockPrimaryRegion)) {
      throw new Error(
        `BEDROCK_PRIMARY_REGION (${bedrockPrimaryRegion}) is not inside ` +
          `STRATOCLAVE_INFERENCE_REGIONS (${inferenceRegions.join(', ')}): the model's own ` +
          `primary region must be reachable within the boundary, or every request fails.`
      );
    }
    // DEFAULT_BEDROCK_MODEL must be explicit under a boundary: the built-in
    // default (DEFAULT_BEDROCK_MODEL_FALLBACK) can never be inside a non-US
    // boundary, and the backend refuses to start on a model the IAM boundary
    // below cannot serve.
    if (env.DEFAULT_BEDROCK_MODEL === undefined) {
      throw new Error(
        `DEFAULT_BEDROCK_MODEL must be set explicitly under STRATOCLAVE_INFERENCE_REGIONS: ` +
          `the built-in default ("${DEFAULT_BEDROCK_MODEL_FALLBACK}") is a US cross-region ` +
          `profile that cannot be called from every boundary, and the backend refuses to start ` +
          `if the configured model cannot be served from within the boundary this module ` +
          `generates IAM for. Set DEFAULT_BEDROCK_MODEL to a profile or directly-hosted model ` +
          `id that is reachable from BEDROCK_PRIMARY_REGION (${bedrockPrimaryRegion}).`
      );
    }
    // `global.` is refused unconditionally, independent of the registry: AWS
    // authorizes a "global." destination call against the region-less
    // foundation-model ARN, and the boundary's Deny statement (lib/ecs-stack.ts)
    // always blocks exactly that ARN, so no region list could ever make a
    // "global." profile reachable under a boundary.
    if (GEO_PROFILE_RE.exec(defaultBedrockModel)?.[1] === 'global') {
      throw new Error(
        `DEFAULT_BEDROCK_MODEL="${defaultBedrockModel}" is a "global." cross-region profile: ` +
          `the IAM this module generates for STRATOCLAVE_INFERENCE_REGIONS explicitly denies ` +
          `the region-less foundation-model ARN a "global." destination call is authorized ` +
          `against (see lib/ecs-stack.ts), so it can never be called under a boundary. Use a ` +
          `region-bounded profile (matching BEDROCK_PRIMARY_REGION) or a directly-hosted model ` +
          `id instead.`
      );
    }
    // Reachability for everything else is read from the bundled model
    // registry (backend/mvp/defaults/models.json) rather than guessed from
    // the id's prefix: an entry's own `bedrock_region` IS the region AWS
    // actually calls it from, so it is compared to BEDROCK_PRIMARY_REGION
    // directly. An id this build has never bundled — a console-activated
    // model, most plausibly — has no such record to check, so it only
    // warns, never throws; verifying it is the operator's job (docs/DEPLOYMENT.md
    // says so).
    const registryEntry = modelRegistry.find(
      (m) => m.bedrock_model_id === defaultBedrockModel
    );
    if (registryEntry === undefined) {
      inferenceBoundaryWarnings.push(
        `[inference-boundary] DEFAULT_BEDROCK_MODEL="${defaultBedrockModel}" is not in the ` +
          `bundled model registry (backend/mvp/defaults/models.json): its Bedrock call region ` +
          `cannot be verified against the boundary here. If it was activated from the console, ` +
          `confirm yourself that it is reachable from BEDROCK_PRIMARY_REGION ` +
          `(${bedrockPrimaryRegion}).`
      );
    } else if (registryEntry.bedrock_region !== bedrockPrimaryRegion) {
      throw new Error(
        `DEFAULT_BEDROCK_MODEL="${defaultBedrockModel}" is recorded in the bundled model ` +
          `registry as calling Bedrock from bedrock_region="${registryEntry.bedrock_region}", ` +
          `not BEDROCK_PRIMARY_REGION (${bedrockPrimaryRegion}). Set DEFAULT_BEDROCK_MODEL to an ` +
          `entry whose bedrock_region matches, or move BEDROCK_PRIMARY_REGION to match it.`
      );
    }
    if (codexEnabled) {
      const codexLeaks = openaiRegistryRegions.filter(
        (r) => !inferenceRegions.includes(r)
      );
      if (codexLeaks.length > 0) {
        throw new Error(
          `STRATOCLAVE_INFERENCE_REGIONS (${inferenceRegions.join(', ')}) excludes ` +
            `${codexLeaks.join(', ')}, which the OpenAI/codex path is hardwired to. OpenAI GPT ` +
            `and xAI models on Bedrock have no Japan-bounded profile — only "us." and ` +
            `"global." — so codex must be off for a boundary like this one; it may stay on ` +
            `for a US boundary that contains both us-east-2 and us-west-2. set ` +
            `STRATOCLAVE_CODEX_ENABLED=false to keep this boundary with codex off, or widen ` +
            `STRATOCLAVE_INFERENCE_REGIONS to include ${codexLeaks.join(', ')}.`
        );
      }
    }
    if (env.STRATOCLAVE_FAILOVER_REGIONS !== undefined) {
      inferenceBoundaryWarnings.push(
        `[inference-boundary] STRATOCLAVE_FAILOVER_REGIONS is set but is ignored under ` +
          `STRATOCLAVE_INFERENCE_REGIONS: the backend turns same-id streaming failover ` +
          `off once a boundary is set (a geo inference profile already fails over ` +
          `inside its own reach, and same-id failover would otherwise be the ONE path ` +
          `that could still cross the boundary).`
      );
    }
    if (!inferenceRegions.includes(bodyRegion)) {
      inferenceBoundaryWarnings.push(
        `[inference-boundary] the deploy region ${bodyRegion} is outside ` +
          `STRATOCLAVE_INFERENCE_REGIONS (${inferenceRegions.join(', ')}): the gateway's ` +
          `own compute and logs are outside the boundary — STRATOCLAVE_INFERENCE_REGIONS ` +
          `covers Bedrock inference only, not where the gateway itself (or CloudFront) runs.`
      );
    }
  }

  // Always surfaced (info, not warning) so a redeploy from a shell that forgot
  // to export STRATOCLAVE_INFERENCE_REGIONS is visible in `cdk synth`/`cdk diff`
  // as a boundary silently widening back to "none", rather than being invisible.
  const inferenceBoundaryInfo =
    `[inference-boundary] effective STRATOCLAVE_INFERENCE_REGIONS: ` +
    (inferenceRegions !== undefined ? inferenceRegions.join(', ') : 'none');

  // The legacy residency analysis below (STRATOCLAVE_RESIDENCY's warn/strict
  // geo-profile and cross-region-leak checks) is SKIPPED ENTIRELY once a
  // boundary is set: it is superseded by the boundary-specific checks above,
  // which already cover the model and the codex path in a boundary-aware
  // way, and two of its inputs change meaning under a boundary — same-id
  // streaming failover is off (the backend ignores STRATOCLAVE_FAILOVER_REGIONS
  // under a boundary, so nothing is forwarded to the container either), and
  // STRATOCLAVE_RESIDENCY cannot be set at the same time as a boundary (see
  // the throw above), so there is nothing left for this analysis to add.
  let failoverRegionsEnv: string | undefined;
  let residencyWarnings: string[];

  if (inferenceRegions !== undefined) {
    failoverRegionsEnv = undefined;
    residencyWarnings = [];
  } else {
    const effectiveFailover = effectiveFailoverRegions(env, bedrockPrimaryRegion);
    for (const r of effectiveFailover) {
      assertRegion('STRATOCLAVE_FAILOVER_REGIONS entry', r);
    }
    failoverRegionsEnv = env.STRATOCLAVE_FAILOVER_REGIONS;

    // Every region a PROMPT can actually reach at runtime, tagged with its source.
    const bedrockCallRegions: { region: string; source: string }[] = [
      { region: bedrockPrimaryRegion, source: 'model' },
      ...effectiveFailover.map((r) => ({ region: r, source: 'failover' })),
      ...(codexEnabled
        ? openaiRegistryRegions.map((r) => ({ region: r, source: 'codex' }))
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

    residencyWarnings = [];
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
        `be made. Use a directly-hosted (region-specific, non-"us./eu./apac./jp./au./ca./global."-` +
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
      if (codexEnabled && openaiRegistryRegions.some((r) => r !== bodyRegion)) {
        hints.push(
          `set STRATOCLAVE_CODEX_ENABLED=false (the OpenAI/codex path is hardwired to ` +
            `${openaiRegistryRegions.join(', ')} in the model registry and cannot be relocated)`
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
  }

  return {
    bodyRegion,
    wafRegion: WAF_REGION,
    bedrockPrimaryRegion,
    defaultBedrockModel,
    failoverRegionsEnv,
    codexEnabled,
    residencyWarnings,
    deprecationWarnings,
    inferenceRegions,
    inferenceBoundaryInfo,
    inferenceBoundaryWarnings,
  };
}
