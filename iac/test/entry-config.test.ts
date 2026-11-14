import { resolveEntryConfig, type Env } from '../lib/entry-config';

/**
 * In-process tests for the entry-mode resolution (lib/entry-config.ts), the
 * pure logic bin/iac.ts calls once to decide which stacks to build. Same
 * convention as region-decoupling.test.ts: no `cdk synth` subprocess here,
 * so these are fast and deterministic.
 */
describe('resolveEntryConfig', () => {
  const REGION = 'us-east-1';

  test('unset STRATOCLAVE_ENTRY defaults to cloudfront', () => {
    expect(resolveEntryConfig({}, REGION)).toEqual({ mode: 'cloudfront' });
  });

  test('explicit empty string STRATOCLAVE_ENTRY also defaults to cloudfront', () => {
    // `??` alone would not catch this (nullish coalescing treats "" as set);
    // an operator's deploy tooling stripping a variable to empty rather than
    // removing it is a real shape, and must behave the same as unset.
    expect(resolveEntryConfig({ STRATOCLAVE_ENTRY: '' }, REGION)).toEqual({ mode: 'cloudfront' });
  });

  test('explicit "cloudfront" resolves the same as unset', () => {
    expect(resolveEntryConfig({ STRATOCLAVE_ENTRY: 'cloudfront' }, REGION)).toEqual({
      mode: 'cloudfront',
    });
  });

  test('is case-insensitive and tolerates surrounding whitespace', () => {
    expect(resolveEntryConfig({ STRATOCLAVE_ENTRY: ' CloudFront ' }, REGION)).toEqual({
      mode: 'cloudfront',
    });
  });

  test('"both" is refused, naming the concrete reason', () => {
    expect(() => resolveEntryConfig({ STRATOCLAVE_ENTRY: 'both' }, REGION)).toThrow(
      /not supported yet/,
    );
    expect(() => resolveEntryConfig({ STRATOCLAVE_ENTRY: 'both' }, REGION)).toThrow(
      /key that works from the internet through CloudFront/,
    );
  });

  test('an unrecognised value is refused and mentions "both" is named but unsupported', () => {
    expect(() => resolveEntryConfig({ STRATOCLAVE_ENTRY: 'nonsense' }, REGION)).toThrow(
      /must be "cloudfront" or "private"/,
    );
  });

  function privateEnv(overrides: Env = {}): Env {
    return {
      STRATOCLAVE_ENTRY: 'private',
      STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: `arn:aws:acm:${REGION}:123456789012:certificate/11111111-2222-3333-4444-555555555555`,
      STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
      STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
      ...overrides,
    };
  }

  test('private mode resolves with all three required values parsed', () => {
    const cfg = resolveEntryConfig(privateEnv(), REGION);
    expect(cfg.mode).toBe('private');
    if (cfg.mode !== 'private') throw new Error('expected private mode');
    expect(cfg.privateEntry).toEqual({
      certificateArn: `arn:aws:acm:${REGION}:123456789012:certificate/11111111-2222-3333-4444-555555555555`,
      domainName: 'stratoclave.internal.example.com',
      allowedPrincipals: ['arn:aws:iam::123456789012:root'],
    });
  });

  test('allowed principals: comma-separated, trimmed, empty entries dropped', () => {
    const cfg = resolveEntryConfig(
      privateEnv({
        STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS:
          ' arn:aws:iam::123456789012:root , arn:aws:iam::123456789012:role/ops-team ,,',
      }),
      REGION,
    );
    if (cfg.mode !== 'private') throw new Error('expected private mode');
    expect(cfg.privateEntry.allowedPrincipals).toEqual([
      'arn:aws:iam::123456789012:root',
      'arn:aws:iam::123456789012:role/ops-team',
    ]);
  });

  test.each([
    ['STRATOCLAVE_PRIVATE_CERTIFICATE_ARN', ''],
    ['STRATOCLAVE_PRIVATE_DOMAIN', ''],
    ['STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS', ''],
  ])('missing %s is refused at synth, naming it', (name, blank) => {
    expect(() => resolveEntryConfig(privateEnv({ [name]: blank }), REGION)).toThrow(
      new RegExp(name),
    );
  });

  test('all three missing at once are named together in one error', () => {
    expect(() =>
      resolveEntryConfig({ STRATOCLAVE_ENTRY: 'private' }, REGION),
    ).toThrow(
      /STRATOCLAVE_PRIVATE_CERTIFICATE_ARN.*STRATOCLAVE_PRIVATE_DOMAIN.*STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS/,
    );
  });

  describe('allowed-principals validation', () => {
    test('a bare "*" is refused', () => {
      expect(() =>
        resolveEntryConfig(privateEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: '*' }), REGION),
      ).toThrow(/must not contain a wildcard/);
    });

    test('"*" mixed with a real principal is still refused (no partial credit for a wildcard)', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({
            STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root,*',
          }),
          REGION,
        ),
      ).toThrow(/must not contain a wildcard/);
    });

    test('role/* is refused (a wildcard embedded in an otherwise ARN-shaped entry)', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({
            STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:role/*',
          }),
          REGION,
        ),
      ).toThrow(/must not contain a wildcard/);
    });

    test('root* is refused (a wildcard suffix disguised as the root principal)', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root*' }),
          REGION,
        ),
      ).toThrow(/must not contain a wildcard/);
    });

    test('arn:aws-cn: is refused (wrong partition)', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws-cn:iam::123456789012:root' }),
          REGION,
        ),
      ).toThrow(/must be aws-partition IAM principal ARNs/);
    });

    test('a principal that is not an IAM ARN at all is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: '123456789012' }),
          REGION,
        ),
      ).toThrow(/must be aws-partition IAM principal ARNs/);
    });

    test('role and user principal ARNs are both accepted, alongside root', () => {
      const cfg = resolveEntryConfig(
        privateEnv({
          STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS:
            'arn:aws:iam::123456789012:role/ops,arn:aws:iam::210987654321:user/alice',
        }),
        REGION,
      );
      if (cfg.mode !== 'private') throw new Error('expected private mode');
      expect(cfg.privateEntry.allowedPrincipals).toEqual([
        'arn:aws:iam::123456789012:role/ops',
        'arn:aws:iam::210987654321:user/alice',
      ]);
    });

    test('a path-qualified role ARN is accepted', () => {
      const cfg = resolveEntryConfig(
        privateEnv({
          STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:role/team/ops-role',
        }),
        REGION,
      );
      if (cfg.mode !== 'private') throw new Error('expected private mode');
      expect(cfg.privateEntry.allowedPrincipals).toEqual([
        'arn:aws:iam::123456789012:role/team/ops-role',
      ]);
    });
  });

  describe('certificate ARN validation', () => {
    test('a certificate in a different region than the deployment is refused, naming the expected region', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({
            STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
              'arn:aws:acm:us-west-2:123456789012:certificate/11111111-2222-3333-4444-555555555555',
          }),
          REGION,
        ),
      ).toThrow(/deployment's own region \(us-east-1\)/);
    });

    test('a malformed certificate ARN is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: 'not-an-arn-at-all' }),
          REGION,
        ),
      ).toThrow(/deployment's own region/);
    });

    test('a certificate ARN for a different resource type is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({
            STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
              'arn:aws:acm:us-east-1:123456789012:something-else/11111111-2222-3333-4444-555555555555',
          }),
          REGION,
        ),
      ).toThrow(/deployment's own region/);
    });
  });

  describe('domain validation', () => {
    test('a domain with a scheme is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'https://stratoclave.internal.example.com' }),
          REGION,
        ),
      ).toThrow(/bare lowercase hostname/);
    });

    test('a domain with a trailing slash is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com/' }),
          REGION,
        ),
      ).toThrow(/bare lowercase hostname/);
    });

    test('an uppercase domain is refused', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'Stratoclave.Internal.Example.com' }),
          REGION,
        ),
      ).toThrow(/bare lowercase hostname/);
    });

    test('a single-label domain is refused (not a valid hostname shape)', () => {
      expect(() =>
        resolveEntryConfig(privateEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave' }), REGION),
      ).toThrow(/bare lowercase hostname/);
    });

    test('a domain with a port is refused (same message as scheme/slash/case)', () => {
      expect(() =>
        resolveEntryConfig(
          privateEnv({ STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com:443' }),
          REGION,
        ),
      ).toThrow(/bare lowercase hostname/);
    });

    test('a well-formed multi-label domain is accepted', () => {
      const cfg = resolveEntryConfig(privateEnv(), REGION);
      if (cfg.mode !== 'private') throw new Error('expected private mode');
      expect(cfg.privateEntry.domainName).toBe('stratoclave.internal.example.com');
    });
  });
});
