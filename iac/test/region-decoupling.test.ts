import * as path from 'path';
import {
  resolveRegionConfig,
  effectiveFailoverRegions,
  DEFAULT_REGION,
  WAF_REGION,
  type Env,
} from '../lib/region-config';

// Fixture registry (shaped like backend/mvp/defaults/models.json) carrying a
// jp.-prefixed entry the REAL bundled file does not have yet, so the
// inference-region boundary's data-driven DEFAULT_BEDROCK_MODEL check can be
// exercised cleanly against a Japan boundary via STRATOCLAVE_MODEL_REGISTRY_PATH.
const FIXTURE_MODEL_REGISTRY_PATH = path.resolve(
  __dirname,
  'fixtures',
  'model-registry-fixture.json',
);

/**
 * In-process tests for the region / residency resolution (lib/region-config.ts),
 * the pure logic extracted from bin/iac.ts. These run without spawning `cdk
 * synth`, so they are fast and deterministic in CI (the earlier subprocess
 * approach raced on a shared cdk.out under parallel jest workers).
 *
 * A minimal env includes CDK_DEFAULT_ACCOUNT so the shape matches production;
 * region logic ignores it. Each case passes a fresh env object (no process.env
 * mutation), so cases are hermetic and order-independent.
 */
function baseEnv(overrides: Env = {}): Env {
  return { CDK_DEFAULT_ACCOUNT: '111122223333', ...overrides };
}

describe('resolveRegionConfig — region decoupling', () => {
  test('us-east-1 default: WAF pinned, model region defaults, residency-silent', () => {
    const cfg = resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1' }));
    expect(cfg.bodyRegion).toBe('us-east-1');
    expect(cfg.wafRegion).toBe(WAF_REGION);
    expect(cfg.bedrockPrimaryRegion).toBe('us-east-1');
    expect(cfg.residencyWarnings).toEqual([]);
  });

  test('unset region falls back to CDK_DEFAULT_REGION then us-east-1', () => {
    expect(resolveRegionConfig(baseEnv()).bodyRegion).toBe(DEFAULT_REGION);
    expect(
      resolveRegionConfig(baseEnv({ CDK_DEFAULT_REGION: 'us-west-2', BEDROCK_PRIMARY_REGION: 'us-west-2' }))
        .bodyRegion,
    ).toBe('us-west-2');
    // STRATOCLAVE_REGION wins over CDK_DEFAULT_REGION.
    expect(
      resolveRegionConfig(
        baseEnv({ STRATOCLAVE_REGION: 'eu-west-1', CDK_DEFAULT_REGION: 'us-west-2', BEDROCK_PRIMARY_REGION: 'eu-west-1' }),
      ).bodyRegion,
    ).toBe('eu-west-1');
  });

  test('BEDROCK_PRIMARY_REGION is independent of the deploy region', () => {
    const cfg = resolveRegionConfig(
      baseEnv({ STRATOCLAVE_REGION: 'eu-west-1', BEDROCK_PRIMARY_REGION: 'us-east-1' }),
    );
    expect(cfg.bodyRegion).toBe('eu-west-1');
    expect(cfg.bedrockPrimaryRegion).toBe('us-east-1');
    // Model != body always warns (bytes leave the deploy region).
    expect(cfg.residencyWarnings.join('\n')).toMatch(/prompt data leaves the deploy region eu-west-1/);
  });

  test('missing BEDROCK_PRIMARY_REGION when body != us-east-1 throws (actionable, mentions bootstrap)', () => {
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'eu-west-1' }))).toThrow(
      /BEDROCK_PRIMARY_REGION must be set/,
    );
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'eu-west-1' }))).toThrow(
      /cdk bootstrap/,
    );
  });

  test('malformed / partition-restricted regions throw', () => {
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'US_EAST_1' }))).toThrow(
      /Invalid deploy region/,
    );
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-gov-east-1', BEDROCK_PRIMARY_REGION: 'us-gov-east-1' }))).toThrow(
      /aws.*partition only/,
    );
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'cn-north-1', BEDROCK_PRIMARY_REGION: 'cn-north-1' }))).toThrow(
      /aws.*partition only/,
    );
    // A bad model region is validated too.
    expect(() => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', BEDROCK_PRIMARY_REGION: 'US_EAST_1' }))).toThrow(
      /Invalid BEDROCK_PRIMARY_REGION/,
    );
  });

  test('STRATOCLAVE_CODEX_ENABLED matches the backend exactly: enabled IFF "true"', () => {
    // Backend (mvp/openai_responses.py) is `.lower() == "true"`. We must match:
    // only "true"/"TRUE" enable; everything else (including 0/no/off) disables.
    // Using `!== 'false'` would flip an existing ..._ENABLED=0 deployment to
    // enabled on the next synth and silently leak codex prompts. (Fable B-1)
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_CODEX_ENABLED: 'true' })).codexEnabled).toBe(true);
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_CODEX_ENABLED: 'TRUE' })).codexEnabled).toBe(true);
    // Any explicit non-"true" value disables codex (backend parity).
    const disabledValues = ['false', 'FALSE', '0', 'no', 'off', ''];
    const computed = disabledValues.map(
      (v) => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_CODEX_ENABLED: v })).codexEnabled,
    );
    expect(computed).toEqual(disabledValues.map(() => false));
  });

  test('STRATOCLAVE_CODEX_ENABLED defaults to true: codex ships on, like the Anthropic route', () => {
    // Codex is not a money/safety gate the way STRATOCLAVE_HARD_CEILING_GATE,
    // STRATOCLAVE_UNOBSERVED_HOLDS, STRATOCLAVE_RESIDENCY, and
    // STRATOCLAVE_RESERVE_PROTOCOL are — every request through it still goes
    // through the same reservation/settlement pipeline and the same
    // pool/quota walls as the Anthropic route. Defaulting it off gates
    // nothing but the CLI's usability: an operator who deploys and never
    // separately discovers and sets this var gets a `stratoclave codex` that
    // 503s for a reason nothing at deploy time mentioned. Codex is one of
    // this gateway's two supported CLIs, not an optional add-on, so an
    // operator who never sets this var gets it ON, matching the backend's
    // own bare-getenv default — the explicit opt-OUT is
    // STRATOCLAVE_CODEX_ENABLED=false (e.g. for strict non-US residency,
    // since the codex path is registry-pinned to us-east-2/us-west-2).
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1' })).codexEnabled).toBe(true);
    expect(resolveRegionConfig(baseEnv()).codexEnabled).toBe(true);
  });

  test('CODEX_ENABLED (deprecated bare name) is honoured as a fallback', () => {
    // An existing deployment that never renamed its env var must not lose the
    // route on the next synth. Precedence: the new name always wins.
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'true' })).codexEnabled).toBe(true);
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'false' })).codexEnabled).toBe(false);
    expect(
      resolveRegionConfig(
        baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'true', STRATOCLAVE_CODEX_ENABLED: 'false' }),
      ).codexEnabled,
    ).toBe(false);
    // Setting the deprecated name surfaces a deprecation warning; setting only
    // the new name (or neither) does not.
    expect(
      resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'true' })).deprecationWarnings
        .join('\n'),
    ).toMatch(/CODEX_ENABLED is deprecated/);
    expect(
      resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_CODEX_ENABLED: 'true' }))
        .deprecationWarnings,
    ).toEqual([]);
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1' })).deprecationWarnings).toEqual([]);
  });
});

describe('effectiveFailoverRegions — residency-safe defaults', () => {
  test('us-east-1 primary, unset: default filtered to the us jurisdiction (eu-west-1 dropped)', () => {
    // Built-in defaults are (us-west-2, eu-west-1); eu-west-1 is a different
    // jurisdiction than the us-* primary, so it is dropped.
    expect(effectiveFailoverRegions({}, 'us-east-1')).toEqual(['us-west-2']);
  });

  test('THE residency bug: eu-west-1 primary, unset, never inherits a US failover', () => {
    // eu-west-1 is also the primary (stripped) and us-west-2 is a different
    // jurisdiction (dropped) -> empty. A US region must NOT appear.
    const fo = effectiveFailoverRegions({}, 'eu-west-1');
    expect(fo).toEqual([]);
    expect(fo.some((r) => r.startsWith('us-'))).toBe(false);
  });

  test('apac primary, unset: no cross-jurisdiction default', () => {
    expect(effectiveFailoverRegions({}, 'ap-northeast-1')).toEqual([]);
  });

  test('explicit list is honoured verbatim across jurisdictions', () => {
    expect(
      effectiveFailoverRegions({ STRATOCLAVE_FAILOVER_REGIONS: 'us-west-2,eu-central-1' }, 'eu-west-1'),
    ).toEqual(['us-west-2', 'eu-central-1']);
  });

  test('disable sentinels and empty/comma-only yield no failover', () => {
    for (const v of ['', 'none', 'disabled', 'off', '  Disabled  ', ',', ' , ']) {
      expect(effectiveFailoverRegions({ STRATOCLAVE_FAILOVER_REGIONS: v }, 'us-east-1')).toEqual([]);
    }
  });

  test('primary is always stripped and duplicates deduped', () => {
    expect(
      effectiveFailoverRegions({ STRATOCLAVE_FAILOVER_REGIONS: 'us-east-1,us-west-2,us-west-2' }, 'us-east-1'),
    ).toEqual(['us-west-2']);
  });
});

describe('resolveRegionConfig — residency (STRATOCLAVE_RESIDENCY)', () => {
  test('strict + geo-profile default model throws (us.anthropic.* cannot certify a region)', () => {
    expect(() =>
      resolveRegionConfig(
        baseEnv({
          STRATOCLAVE_REGION: 'eu-west-1',
          BEDROCK_PRIMARY_REGION: 'eu-west-1',
          STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
          STRATOCLAVE_CODEX_ENABLED: 'false',
          STRATOCLAVE_RESIDENCY: 'strict',
        }),
      ),
    ).toThrow(/geo (cross-region )?inference profile/i);
  });

  test('strict + geo-profile + escape hatch downgrades to a warning', () => {
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'eu-west-1',
        BEDROCK_PRIMARY_REGION: 'eu-west-1',
        STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
        STRATOCLAVE_CODEX_ENABLED: 'false',
        STRATOCLAVE_RESIDENCY: 'strict',
        STRATOCLAVE_ALLOW_GEO_INFERENCE: 'true',
      }),
    );
    expect(cfg.residencyWarnings.join('\n')).toMatch(/geo cross-region inference profile/i);
  });

  test('full EU residency with a directly-hosted model is strict-clean', () => {
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'eu-west-1',
        BEDROCK_PRIMARY_REGION: 'eu-west-1',
        STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
        STRATOCLAVE_CODEX_ENABLED: 'false',
        STRATOCLAVE_RESIDENCY: 'strict',
        DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
      }),
    );
    expect(cfg.residencyWarnings).toEqual([]);
  });

  test('NEW-1: codex enabled defeats residency even with everything else pinned', () => {
    // OPENAI_BEDROCK_REGIONS is a no-op hint; codex is registry-pinned to
    // us-west-2/us-east-2, so strict must still throw. STRATOCLAVE_CODEX_ENABLED
    // is passed EXPLICITLY here even though codex now defaults to true too —
    // this test's point is that pinning it true (as an operator migrating an
    // old, explicitly-configured deployment would) still defeats strict
    // residency; the no-env-var case is covered by the next test.
    expect(() =>
      resolveRegionConfig(
        baseEnv({
          STRATOCLAVE_REGION: 'eu-west-1',
          BEDROCK_PRIMARY_REGION: 'eu-west-1',
          STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
          DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
          STRATOCLAVE_RESIDENCY: 'strict',
          OPENAI_BEDROCK_REGIONS: 'eu-west-1',
          STRATOCLAVE_CODEX_ENABLED: 'true',
        }),
      ),
    ).toThrow(/us-west-2\(codex\)|us-east-2\(codex\)|Bedrock is reachable/);
  });

  test('NEW-9: codex defaulting to true now surfaces on a non-US body region even with no codex var set at all', () => {
    // Direct consequence of the default flip (contract R1 for this change):
    // before, an operator who deployed to a non-US region and never touched
    // any codex var got a residency-silent synth, because codex defaulted
    // off and never reached the US registry regions. Now the same env
    // produces a warning (non-strict) because codex defaults ON and its
    // registry pin (us-east-2/us-west-2) is unconditionally reachable unless
    // explicitly disabled. This is not a regression to paper over: it is the
    // residency analysis correctly describing what the deployment will
    // actually do at runtime.
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'eu-west-1',
        BEDROCK_PRIMARY_REGION: 'eu-west-1',
        STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
        DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
        // No STRATOCLAVE_RESIDENCY, no STRATOCLAVE_CODEX_ENABLED: residency
        // intent still triggers off the non-default body region alone.
      }),
    );
    expect(cfg.codexEnabled).toBe(true);
    expect(cfg.residencyWarnings.join('\n')).toMatch(/us-west-2\(codex\)|us-east-2\(codex\)/);
    // Same env, but STRATOCLAVE_RESIDENCY=strict: the warning becomes a hard
    // synth failure, forcing the explicit opt-out this deployment needs.
    expect(() =>
      resolveRegionConfig(
        baseEnv({
          STRATOCLAVE_REGION: 'eu-west-1',
          BEDROCK_PRIMARY_REGION: 'eu-west-1',
          STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
          DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
          STRATOCLAVE_RESIDENCY: 'strict',
        }),
      ),
    ).toThrow(/us-west-2\(codex\)|us-east-2\(codex\)|Bedrock is reachable/);
  });

  test('NEW-6: invalid STRATOCLAVE_RESIDENCY value throws', () => {
    expect(() =>
      resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_RESIDENCY: 'strickt' })),
    ).toThrow(/STRATOCLAVE_RESIDENCY must be/);
  });

  test('us-east-1 default deploy is residency-silent (backward compatible)', () => {
    // No residency intent (default region, no STRATOCLAVE_RESIDENCY) -> the
    // default us-west-2 failover + us codex do not produce warnings.
    const cfg = resolveRegionConfig(baseEnv());
    expect(cfg.residencyWarnings).toEqual([]);
  });

  test('jp. / au. / ca. are recognized as geo inference profiles under strict residency', () => {
    for (const prefix of ['jp', 'au', 'ca']) {
      expect(() =>
        resolveRegionConfig(
          baseEnv({
            STRATOCLAVE_REGION: 'us-east-1',
            STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
            STRATOCLAVE_CODEX_ENABLED: 'false',
            STRATOCLAVE_RESIDENCY: 'strict',
            DEFAULT_BEDROCK_MODEL: `${prefix}.anthropic.claude-opus-5`,
          }),
        ),
      ).toThrow(/geo cross-region inference profile/i);
    }
  });
});

describe('resolveRegionConfig — inference-region boundary (STRATOCLAVE_INFERENCE_REGIONS)', () => {
  // A Japan boundary needs codex explicitly off: codex's registry pin
  // (us-west-2/us-east-2) is never inside a Japan-only boundary. It also
  // needs an explicit, jp.-compatible DEFAULT_BEDROCK_MODEL: the built-in
  // default is a "us." profile, which is refused under any non-US boundary.
  // STRATOCLAVE_MODEL_REGISTRY_PATH points at the fixture registry (which
  // carries a jp. entry the real bundled file does not have yet) so this
  // model resolves cleanly, with no "not in the bundled registry" warning,
  // for every test below that only cares about the OTHER boundary checks.
  function japanBoundaryEnv(overrides: Env = {}): Env {
    return baseEnv({
      STRATOCLAVE_REGION: 'ap-northeast-1',
      BEDROCK_PRIMARY_REGION: 'ap-northeast-1',
      STRATOCLAVE_INFERENCE_REGIONS: 'ap-northeast-1,ap-northeast-3',
      STRATOCLAVE_CODEX_ENABLED: 'false',
      DEFAULT_BEDROCK_MODEL: 'jp.anthropic.claude-opus-4-7',
      STRATOCLAVE_MODEL_REGISTRY_PATH: FIXTURE_MODEL_REGISTRY_PATH,
      ...overrides,
    });
  }

  test('unset: no boundary, inferenceBoundaryInfo says "none", no warnings', () => {
    const cfg = resolveRegionConfig(baseEnv());
    expect(cfg.inferenceRegions).toBeUndefined();
    expect(cfg.inferenceBoundaryInfo).toMatch(/none/);
    expect(cfg.inferenceBoundaryWarnings).toEqual([]);
  });

  test('set: parsed, trimmed, order preserved; info names the effective boundary', () => {
    const cfg = resolveRegionConfig(
      japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: ' ap-northeast-1 , ap-northeast-3 ' }),
    );
    expect(cfg.inferenceRegions).toEqual(['ap-northeast-1', 'ap-northeast-3']);
    expect(cfg.inferenceBoundaryInfo).toMatch(/ap-northeast-1, ap-northeast-3/);
  });

  test('empty string throws (treated as a blank entry, not "unset")', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: '' })),
    ).toThrow(/STRATOCLAVE_INFERENCE_REGIONS must be a non-empty/);
  });

  test('a blank entry throws', () => {
    expect(() =>
      resolveRegionConfig(
        japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: 'ap-northeast-1,,ap-northeast-3' }),
      ),
    ).toThrow(/STRATOCLAVE_INFERENCE_REGIONS must be a non-empty/);
  });

  test('a duplicate entry throws', () => {
    expect(() =>
      resolveRegionConfig(
        japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: 'ap-northeast-1,ap-northeast-1' }),
      ),
    ).toThrow(/lists "ap-northeast-1" more than once/);
  });

  test('a malformed region entry throws via assertRegion', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: 'JAPAN' })),
    ).toThrow(/STRATOCLAVE_INFERENCE_REGIONS entry/);
  });

  test('BEDROCK_PRIMARY_REGION outside the boundary throws, naming both', () => {
    expect(() =>
      resolveRegionConfig(
        japanBoundaryEnv({ STRATOCLAVE_INFERENCE_REGIONS: 'ap-northeast-3' }),
      ),
    ).toThrow(/BEDROCK_PRIMARY_REGION \(ap-northeast-1\) is not inside STRATOCLAVE_INFERENCE_REGIONS \(ap-northeast-3\)/);
  });

  test('DEFAULT_BEDROCK_MODEL unset under a boundary throws (the US default cannot serve a non-US boundary)', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ DEFAULT_BEDROCK_MODEL: undefined })),
    ).toThrow(/DEFAULT_BEDROCK_MODEL must be set explicitly under STRATOCLAVE_INFERENCE_REGIONS/);
  });

  test('a "global." DEFAULT_BEDROCK_MODEL under a boundary throws', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ DEFAULT_BEDROCK_MODEL: 'global.anthropic.claude-opus-5' })),
    ).toThrow(/"global\." cross-region profile/);
  });

  test('a registry entry whose bedrock_region matches BEDROCK_PRIMARY_REGION is accepted, no warning', () => {
    // Via the real bundled file: us.anthropic.claude-opus-5's bedrock_region
    // is us-east-1, matching a US boundary's primary.
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'us-east-1',
        STRATOCLAVE_INFERENCE_REGIONS: 'us-east-1',
        STRATOCLAVE_CODEX_ENABLED: 'false',
        DEFAULT_BEDROCK_MODEL: 'us.anthropic.claude-opus-5',
      }),
    );
    expect(cfg.inferenceBoundaryWarnings).toEqual([]);

    // Via the fixture registry: jp.anthropic.claude-opus-4-7's bedrock_region
    // is ap-northeast-1, matching the Japan boundary's primary.
    const jpCfg = resolveRegionConfig(japanBoundaryEnv());
    expect(jpCfg.inferenceBoundaryWarnings).toEqual([]);
  });

  test('a registry entry whose bedrock_region does NOT match BEDROCK_PRIMARY_REGION throws', () => {
    // us.anthropic.claude-opus-5 is recorded at bedrock_region=us-east-1, so
    // it cannot be reached from a Japan boundary's primary (ap-northeast-1).
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ DEFAULT_BEDROCK_MODEL: 'us.anthropic.claude-opus-5' })),
    ).toThrow(
      /recorded in the bundled model registry as calling Bedrock from bedrock_region="us-east-1", not BEDROCK_PRIMARY_REGION \(ap-northeast-1\)/,
    );
  });

  test('a DEFAULT_BEDROCK_MODEL not in the bundled registry warns instead of throwing', () => {
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'us-east-1',
        STRATOCLAVE_INFERENCE_REGIONS: 'us-east-1',
        STRATOCLAVE_CODEX_ENABLED: 'false',
        // Not in backend/mvp/defaults/models.json at all -- e.g. a console-activated model.
        DEFAULT_BEDROCK_MODEL: 'anthropic.claude-console-activated-example',
      }),
    );
    expect(cfg.inferenceBoundaryWarnings.join('\n')).toMatch(
      /is not in the bundled model registry \(backend\/mvp\/defaults\/models\.json\)/,
    );
  });

  test('STRATOCLAVE_MODEL_REGISTRY_PATH overrides which file is read', () => {
    // Pointed at the fixture, jp.anthropic.claude-opus-4-7 resolves (no
    // warning) even though it is not in the real bundled file.
    const cfg = resolveRegionConfig(japanBoundaryEnv());
    expect(
      cfg.inferenceBoundaryWarnings.some((w) => w.includes('not in the bundled model registry')),
    ).toBe(false);
  });

  test('a bare (directly-hosted) DEFAULT_BEDROCK_MODEL not in the registry warns rather than throws', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6' })),
    ).not.toThrow();
  });

  test('codex enabled with a registry region outside the boundary throws, naming the opt-out', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_CODEX_ENABLED: 'true' })),
    ).toThrow(/STRATOCLAVE_CODEX_ENABLED=false/);
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_CODEX_ENABLED: 'true' })),
    ).toThrow(/us-west-2|us-east-2/);
  });

  test('the codex region exclusion reads the REAL bundled registry when STRATOCLAVE_MODEL_REGISTRY_PATH is unset', () => {
    // No fixture override here, and codex is left at its true-by-default
    // value, so this only passes if the codex-region set this module derived
    // from backend/mvp/defaults/models.json still includes us-east-2/us-west-2.
    expect(() =>
      resolveRegionConfig(
        baseEnv({
          STRATOCLAVE_REGION: 'ap-northeast-1',
          BEDROCK_PRIMARY_REGION: 'ap-northeast-1',
          STRATOCLAVE_INFERENCE_REGIONS: 'ap-northeast-1,ap-northeast-3',
          // DEFAULT_BEDROCK_MODEL deliberately not in the real file -- this test is
          // only about the codex check, so the model check must warn, not throw.
          DEFAULT_BEDROCK_MODEL: 'anthropic.claude-console-activated-example',
        }),
      ),
    ).toThrow(/us-west-2|us-east-2/);
  });

  test('codex explicitly disabled under the same boundary does not throw', () => {
    expect(() => resolveRegionConfig(japanBoundaryEnv())).not.toThrow();
  });

  test('STRATOCLAVE_FAILOVER_REGIONS set under a boundary warns that it is ignored', () => {
    const cfg = resolveRegionConfig(
      japanBoundaryEnv({ STRATOCLAVE_FAILOVER_REGIONS: 'disabled' }),
    );
    expect(cfg.inferenceBoundaryWarnings.join('\n')).toMatch(
      /STRATOCLAVE_FAILOVER_REGIONS is set but is ignored/,
    );
  });

  test('STRATOCLAVE_FAILOVER_REGIONS unset under a boundary produces no such warning', () => {
    const cfg = resolveRegionConfig(japanBoundaryEnv());
    expect(cfg.inferenceBoundaryWarnings).toEqual([]);
  });

  test('a deploy region outside the boundary warns that the gateway itself sits outside it', () => {
    const cfg = resolveRegionConfig(
      baseEnv({
        STRATOCLAVE_REGION: 'us-west-2',
        BEDROCK_PRIMARY_REGION: 'us-east-1',
        STRATOCLAVE_INFERENCE_REGIONS: 'us-east-1',
        STRATOCLAVE_CODEX_ENABLED: 'false',
        DEFAULT_BEDROCK_MODEL: 'us.anthropic.claude-opus-5',
      }),
    );
    expect(cfg.inferenceBoundaryWarnings.join('\n')).toMatch(
      /deploy region us-west-2 is outside STRATOCLAVE_INFERENCE_REGIONS/,
    );
  });

  test('a deploy region inside the boundary produces no such warning', () => {
    const cfg = resolveRegionConfig(japanBoundaryEnv());
    expect(cfg.inferenceBoundaryWarnings).toEqual([]);
  });

  test('STRATOCLAVE_RESIDENCY set together with STRATOCLAVE_INFERENCE_REGIONS throws, naming which to keep', () => {
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_RESIDENCY: 'warn' })),
    ).toThrow(/STRATOCLAVE_INFERENCE_REGIONS and STRATOCLAVE_RESIDENCY cannot both be set/);
    expect(() =>
      resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_RESIDENCY: 'strict' })),
    ).toThrow(/Keep STRATOCLAVE_INFERENCE_REGIONS/);
  });

  test('under a boundary, the legacy residency analysis is skipped entirely: residencyWarnings is always []', () => {
    // Even configurations that would have produced legacy [residency] warnings
    // without a boundary (a non-default body region, a geo-profile model)
    // produce none here -- that analysis does not run once a boundary is set.
    const cfg = resolveRegionConfig(japanBoundaryEnv());
    expect(cfg.residencyWarnings).toEqual([]);
  });

  test('under a boundary, STRATOCLAVE_FAILOVER_REGIONS is never forwarded to the container, even when set', () => {
    const cfg = resolveRegionConfig(japanBoundaryEnv({ STRATOCLAVE_FAILOVER_REGIONS: 'us-west-2' }));
    expect(cfg.failoverRegionsEnv).toBeUndefined();
  });
});
