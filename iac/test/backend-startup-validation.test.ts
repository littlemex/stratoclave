/**
 * Proves `ENVIRONMENT=production` (always set on the container now,
 * independent of the resource tier -- see `backend-deployed-flag.test.ts`)
 * does not crash a FRESH task on startup, for every combination of resource
 * tier and entry mode this app can synthesize. Earlier tests proved the
 * container RECEIVES `ENVIRONMENT=production`; this one proves the backend
 * accepts the REST of what it receives alongside it, by actually running
 * `main.py`'s own module-level startup validation (the
 * `_REQUIRED_IN_PRODUCTION` env list and the CORS checks, both of which
 * `environment == "production"` newly activates for every deployment) in a
 * real Python subprocess against the REAL synthesized container environment
 * -- not a hand-picked subset of it.
 *
 * A synthesized CloudFormation template cannot contain the FINAL value of
 * anything derived from another resource (a Cognito pool id, a table name
 * composed with the stack's own generated suffix): those appear as
 * intrinsic-function objects (`{"Fn::GetAtt": [...]}`, `{"Ref": "..."}`,
 * `{"Fn::Join": [...]}`), not strings, because CloudFormation itself
 * resolves them only at deploy time. `main.py`'s validation only checks
 * PRESENCE and SHAPE (non-empty; CORS_ORIGINS looks like a URL and is not
 * `localhost`), never the real value, so a placeholder string in each slot
 * exercises the same code path a real deploy would reach -- this is "every
 * key `cdk deploy` would set is actually set, non-empty, and the right
 * rough shape", not "the real Cognito pool exists".
 *
 * REQUIRES `backend/.venv` to already exist with `requirements.txt` AND
 * `requirements-dev.txt` installed (see this repo's own CONTRIBUTING.md /
 * the commands this change's own report names) -- this file shells out to
 * that interpreter rather than bundling a second Python environment.
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { hermeticSynthEnv } from './_hermetic-synth';

const iacDir = path.resolve(__dirname, '..');
const backendDir = path.resolve(iacDir, '..', 'backend');
const backendPython = path.join(backendDir, '.venv', 'bin', 'python3');

type CfnTemplate = { Resources?: Record<string, any> };

function synthEcsTemplate(prefix: string, extraEnv: Record<string, string>): CfnTemplate {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-startup-validation-'));
  try {
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env: hermeticSynthEnv({
        CDK_DEFAULT_ACCOUNT: '123456789012',
        CDK_DEFAULT_REGION: 'us-east-1',
        STRATOCLAVE_REGION: 'us-east-1',
        STRATOCLAVE_PREFIX: prefix,
        IMAGE_TAG: 'startup-validation-test',
        CDK_NAG: 'off',
        ...extraEnv,
      }),
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    return JSON.parse(fs.readFileSync(path.join(outDir, `${prefix}-ecs.template.json`), 'utf-8'));
  } finally {
    fs.rmSync(outDir, { recursive: true, force: true });
  }
}

/** Every `Name`/`Value` pair off the backend container, duplicates rejected
 * (same reasoning as `documented-env-wiring.test.ts::backendContainerEnv`),
 * but the raw `Value` kept AS-IS -- a string for a literal, an object for an
 * unresolved intrinsic -- so the caller decides how to placeholder it. */
function rawBackendContainerEnv(template: CfnTemplate): Record<string, unknown> {
  const taskDefs = Object.values(template.Resources || {}).filter(
    (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
  );
  expect(taskDefs).toHaveLength(1);
  const containers = (taskDefs[0] as any).Properties?.ContainerDefinitions || [];
  const backend = containers.find((c: any) => /backend/i.test(String(c.Name)));
  expect(backend).toBeDefined();
  const env: Record<string, unknown> = {};
  for (const pair of backend.Environment || []) {
    env[pair.Name] = pair.Value;
  }
  return env;
}

/** Keys whose placeholder must look like the real thing's SHAPE, not just be
 * a non-empty string, because `main.py`'s own validation reads that shape
 * (a URL scheme, no `localhost`) rather than only checking presence. Every
 * other unresolved key gets a generic placeholder below. */
const SHAPED_PLACEHOLDERS: Record<string, string> = {
  CORS_ORIGINS: 'https://placeholder.example.com',
  STRATOCLAVE_API_ENDPOINT: 'https://placeholder.example.com',
};

/** Resolve every value in a raw container env to a plain string a subprocess
 * `env` can actually use: a literal stays as-is; an unresolved intrinsic
 * (any non-string -- JSON.parse turned `{"Fn::GetAtt": [...]}` etc. into a
 * real object) becomes the shaped placeholder for its key, or a generic one. */
function placeholderize(raw: Record<string, unknown>): Record<string, string> {
  const resolved: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw)) {
    if (typeof value === 'string') {
      resolved[key] = value;
    } else {
      resolved[key] = SHAPED_PLACEHOLDERS[key] || 'placeholder-value';
    }
  }
  return resolved;
}

/** Run `main.py`'s own module-level startup validation against a given
 * container environment, in a real subprocess (so a crash there is a
 * subprocess exit code, not an exception inside the test's own process,
 * and so nothing this test does leaks into any other test's Python
 * import cache). Returns the exit code and combined output for a message
 * that can actually be debugged. */
function runStartupValidation(containerEnv: Record<string, string>): { code: number; output: string } {
  try {
    const output = execFileSync(
      backendPython,
      ['-c', 'import main'],
      {
        cwd: backendDir,
        env: { ...containerEnv, PATH: process.env.PATH || '' },
        encoding: 'utf-8',
        stdio: 'pipe',
        timeout: 60_000,
      },
    );
    return { code: 0, output };
  } catch (e: unknown) {
    const err = e as { status?: number; stdout?: string; stderr?: string };
    return { code: err.status ?? 1, output: `${err.stdout ?? ''}\n${err.stderr ?? ''}` };
  }
}

describe('a fresh deployment starts under ENVIRONMENT=production, for every tier and entry mode', () => {
  if (!fs.existsSync(backendPython)) {
    test.skip('backend/.venv is not set up -- see this file\'s own header comment', () => {});
    return;
  }

  const cases: Array<[string, Record<string, string>]> = [
    ['cloudfront entry, dev resource tier', {}],
    ['cloudfront entry, prod resource tier', { ENVIRONMENT: 'production' }],
    [
      'private entry, dev resource tier',
      {
        STRATOCLAVE_ENTRY: 'private',
        STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
          'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
        STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
      },
    ],
    [
      'private entry, prod resource tier',
      {
        ENVIRONMENT: 'production',
        STRATOCLAVE_ENTRY: 'private',
        STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
          'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
        STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
      },
    ],
    // `STRATOCLAVE_ENTRY=both`: CloudFront (via a VPC origin) and PrivateLink
    // share one internal ALB and one backend (see lib/entry-config.ts's
    // `EntryConfig` doc). Sets STRATOCLAVE_API_ENDPOINT_PRIVATE and
    // STRATOCLAVE_STS_AUDIENCES on the container in addition to everything
    // `private` sets -- `core.deployment.validate_api_endpoint_private` and
    // `mvp.sso_sts.validate_audience_config` both run at startup against
    // these, so this is the one case that actually exercises them with a
    // non-empty value rather than the all-other-modes "unset, no-op" path.
    [
      'both entry, dev resource tier',
      {
        STRATOCLAVE_ENTRY: 'both',
        STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
          'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
        STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
      },
    ],
    [
      'both entry, prod resource tier',
      {
        ENVIRONMENT: 'production',
        STRATOCLAVE_ENTRY: 'both',
        STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
          'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
        STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
        STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
      },
    ],
    // `STRATOCLAVE_CLOUDFRONT_ORIGIN`'s other states (lib/entry-config.ts's
    // `CloudfrontOriginMode` doc): `vpc` is the implicit default already
    // covered by the two plain `cloudfront entry` cases above (unset ->
    // `vpc` since this feature landed); these three cover the explicit
    // fallback and the two migration steps, none of which change any
    // backend-visible env key or value -- only lib/alb-stack.ts's/
    // lib/network-stack.ts's own resources differ -- so this is really
    // "synth still succeeds and the container env this test reads is
    // unaffected", not a new code path in main.py's own validation.
    ['cloudfront entry, explicit public fallback', { STRATOCLAVE_CLOUDFRONT_ORIGIN: 'public' }],
    ['cloudfront entry, vpc-expand migration step', { STRATOCLAVE_CLOUDFRONT_ORIGIN: 'vpc-expand' }],
    ['cloudfront entry, vpc-switch migration step', { STRATOCLAVE_CLOUDFRONT_ORIGIN: 'vpc-switch' }],
  ];

  test.each(cases)('%s', (_label, synthEnv) => {
    const prefix = `startup${Math.random().toString(36).slice(2, 8)}`;
    const template = synthEcsTemplate(prefix, synthEnv);
    const containerEnv = placeholderize(rawBackendContainerEnv(template));
    expect(containerEnv.ENVIRONMENT).toBe('production');

    const { code, output } = runStartupValidation(containerEnv);
    if (code !== 0) {
      throw new Error(`main.py failed its own startup validation:\n${output}`);
    }
  }, 180_000);

  test('a fresh container env actually names every _REQUIRED_IN_PRODUCTION variable (regression pin, read from main.py itself rather than hand-copied)', () => {
    const requiredList = fs
      .readFileSync(path.join(backendDir, 'main.py'), 'utf-8')
      .split('_REQUIRED_IN_PRODUCTION = [', 2)[1]
      .split(']', 2)[0];
    const required = [...requiredList.matchAll(/"([A-Z0-9_]+)"/g)].map((m) => m[1]);
    expect(required.length).toBeGreaterThan(3);

    const prefix = `reqenv${Math.random().toString(36).slice(2, 8)}`;
    const containerEnv = rawBackendContainerEnv(synthEcsTemplate(prefix, {}));
    const missing = required.filter((name) => !(name in containerEnv));
    expect(missing).toEqual([]);
  }, 120_000);
});
