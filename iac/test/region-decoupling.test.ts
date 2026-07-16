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

  test('CODEX_ENABLED matches the backend exactly: enabled IFF "true"', () => {
    // Backend (mvp/openai_responses.py) is `.lower() == "true"`. We must match:
    // only "true"/"TRUE" enable; everything else (including 0/no/off) disables.
    // Using `!== 'false'` would flip an existing CODEX_ENABLED=0 deployment to
    // enabled on the next synth and silently leak codex prompts. (Fable B-1)
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1' })).codexEnabled).toBe(true);
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'true' })).codexEnabled).toBe(true);
    expect(resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: 'TRUE' })).codexEnabled).toBe(true);
    // Any explicit non-"true" value disables codex (backend parity). NOTE:
    // empty string is falsy in JS `||`, so it takes the IaC default ('true') —
    // the container then receives 'true' too (String(codexEnabled)), so IaC and
    // the task agree. An operator disabling codex uses 'false', not ''.
    const disabledValues = ['false', 'FALSE', '0', 'no', 'off'];
    const computed = disabledValues.map(
      (v) => resolveRegionConfig(baseEnv({ STRATOCLAVE_REGION: 'us-east-1', CODEX_ENABLED: v })).codexEnabled,
    );
    expect(computed).toEqual(disabledValues.map(() => false));
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
          CODEX_ENABLED: 'false',
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
        CODEX_ENABLED: 'false',
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
        CODEX_ENABLED: 'false',
        STRATOCLAVE_RESIDENCY: 'strict',
        DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
      }),
    );
    expect(cfg.residencyWarnings).toEqual([]);
  });

  test('NEW-1: codex enabled defeats residency even with everything else pinned', () => {
    // OPENAI_BEDROCK_REGIONS is a no-op hint; codex is registry-pinned to
    // us-west-2/us-east-2, so strict must still throw.
    expect(() =>
      resolveRegionConfig(
        baseEnv({
          STRATOCLAVE_REGION: 'eu-west-1',
          BEDROCK_PRIMARY_REGION: 'eu-west-1',
          STRATOCLAVE_FAILOVER_REGIONS: 'disabled',
          DEFAULT_BEDROCK_MODEL: 'anthropic.claude-sonnet-4-6',
          STRATOCLAVE_RESIDENCY: 'strict',
          OPENAI_BEDROCK_REGIONS: 'eu-west-1',
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
