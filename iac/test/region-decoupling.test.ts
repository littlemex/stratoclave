import {
  resolveRegionConfig,
  effectiveFailoverRegions,
  DEFAULT_REGION,
  WAF_REGION,
  type Env,
} from '../lib/region-config';

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
});
