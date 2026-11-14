/**
 * The backend container's own `ENVIRONMENT` must say "this process has been
 * deployed", unconditionally -- never the resource-tier `envName` `bin/iac.ts`
 * also computes (deletion protection, PITR, retain-on-delete, ECS exec
 * default). Before this file, a `cdk deploy` run with no `ENVIRONMENT`
 * exported -- the default, and the common case, since the documented
 * deploy procedure (`deploy-all.sh`) never sets it -- passed `ENVIRONMENT:
 * 'development'` to the container. The backend reads that single variable
 * for two UNRELATED questions: `core.deployment.is_local_environment()`
 * (STRATOCLAVE_API_ENDPOINT required, `/.well-known` headers refused) and
 * every bare `environment == "production"` check in `main.py` (the
 * `_REQUIRED_IN_PRODUCTION` env list, the `CORS_ORIGINS` no-`localhost`
 * check, the `http://` CORS refusal) and `mvp/authz.py` (the admin-creation
 * bootstrap window's `ALLOW_ADMIN_CREATION_UNTIL` expiry requirement). A
 * deployed-but-"development"-labelled gateway skipped every one of those.
 *
 * This file proves the fix holds through the REAL app (`bin/iac.ts`), not
 * through `EcsStack` built in isolation -- the defect was in the one line
 * composing `envName` into the container's environment map, which a
 * stack-level unit test passing its own `environment` prop would never see
 * fail. Same style as `test/documented-env-wiring.test.ts` and
 * `test/private-entry.test.ts`.
 */
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { hermeticSynthEnv } from './_hermetic-synth';

const iacDir = path.resolve(__dirname, '..');

type CfnTemplate = { Resources?: Record<string, any> };

function synth(prefix: string, extraEnv: Record<string, string>): CfnTemplate {
  const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-deployed-flag-'));
  try {
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env: hermeticSynthEnv({
        CDK_DEFAULT_ACCOUNT: '123456789012',
        CDK_DEFAULT_REGION: 'us-east-1',
        STRATOCLAVE_REGION: 'us-east-1',
        STRATOCLAVE_PREFIX: prefix,
        IMAGE_TAG: 'deployed-flag-test',
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

/** The environment of the container that runs the backend, by name -- same
 * helper and the same reasoning (duplicate keys rejected, not collapsed) as
 * `test/documented-env-wiring.test.ts::backendContainerEnv`. */
function backendContainerEnv(template: CfnTemplate): Record<string, unknown> {
  const taskDefs = Object.values(template.Resources || {}).filter(
    (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
  );
  expect(taskDefs).toHaveLength(1);
  const containers = (taskDefs[0] as any).Properties?.ContainerDefinitions || [];
  const backend = containers.find((c: any) => /backend/i.test(String(c.Name)));
  expect(backend).toBeDefined();
  const env: Record<string, unknown> = {};
  const seen = new Set<string>();
  const duplicates: string[] = [];
  for (const pair of backend.Environment || []) {
    if (seen.has(pair.Name)) duplicates.push(pair.Name);
    seen.add(pair.Name);
    env[pair.Name] = pair.Value;
  }
  expect(duplicates).toEqual([]);
  return env;
}

function tableDeletionProtection(template: CfnTemplate, prefix: string, dynamoTemplate: CfnTemplate): boolean[] {
  const tables = Object.values(dynamoTemplate.Resources || {}).filter(
    (r: any) => r.Type === 'AWS::DynamoDB::Table',
  );
  expect(tables.length).toBeGreaterThan(5);
  return tables.map((t: any) => Boolean(t.Properties?.DeletionProtectionEnabled));
}

describe('the backend container ENVIRONMENT says "deployed", not the resource tier', () => {
  // Three operator choices for the SYNTH-TIME `ENVIRONMENT` (the resource-tier
  // knob): unset (today's real-world default -- `deploy-all.sh` never sets
  // it), 'development' explicitly, and 'production' explicitly. All three
  // MUST still produce a container env with `ENVIRONMENT: 'production'`.
  test.each([
    ['unset (the default `deploy-all.sh` leaves it at)', {}],
    ['development', { ENVIRONMENT: 'development' }],
    ['production', { ENVIRONMENT: 'production' }],
  ])('cloudfront entry, synth-time ENVIRONMENT=%s', (_label, extraEnv) => {
    const prefix = `envflag${Math.random().toString(36).slice(2, 8)}`;
    const template = synth(prefix, extraEnv as Record<string, string>);
    const env = backendContainerEnv(template);
    // Strict equality, not merely "truthy" or "not literally
    // 'development'": the exact literal this file's own docstring claims,
    // regardless of what the synth-time resource-tier knob was set to.
    expect(env.ENVIRONMENT).toBe('production');
  }, 120_000);

  test('private entry also always gets ENVIRONMENT=production on the container', () => {
    const prefix = `envflagpriv${Math.random().toString(36).slice(2, 6)}`;
    const certArn = `arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555`;
    const template = synth(prefix, {
      STRATOCLAVE_ENTRY: 'private',
      STRATOCLAVE_PRIVATE_CERTIFICATE_ARN: certArn,
      STRATOCLAVE_PRIVATE_DOMAIN: 'stratoclave.internal.example.com',
      STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
    });
    const env = backendContainerEnv(template);
    expect(env.ENVIRONMENT).toBe('production');
  }, 120_000);

  test('the resource tier (DynamoDB deletion protection) still follows the synth-time ENVIRONMENT, independently', () => {
    // The point of separating the two is that THIS still works exactly as
    // before -- a disposable sandbox stack can still opt out of deletion
    // protection -- while the container's own ENVIRONMENT no longer rides
    // along with it.
    const devPrefix = `envfdev${Math.random().toString(36).slice(2, 6)}`;
    const prodPrefix = `envfprod${Math.random().toString(36).slice(2, 6)}`;
    const outDirDev = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-deployed-flag-ddb-'));
    const outDirProd = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-deployed-flag-ddb-'));
    try {
      execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDirDev, '--quiet'], {
        cwd: iacDir,
        env: hermeticSynthEnv({
          CDK_DEFAULT_ACCOUNT: '123456789012', CDK_DEFAULT_REGION: 'us-east-1',
          STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_PREFIX: devPrefix,
          IMAGE_TAG: 't', CDK_NAG: 'off', ENVIRONMENT: 'development',
        }),
        encoding: 'utf-8', stdio: 'pipe',
      });
      execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDirProd, '--quiet'], {
        cwd: iacDir,
        env: hermeticSynthEnv({
          CDK_DEFAULT_ACCOUNT: '123456789012', CDK_DEFAULT_REGION: 'us-east-1',
          STRATOCLAVE_REGION: 'us-east-1', STRATOCLAVE_PREFIX: prodPrefix,
          IMAGE_TAG: 't', CDK_NAG: 'off', ENVIRONMENT: 'production',
        }),
        encoding: 'utf-8', stdio: 'pipe',
      });
      const devDdb = JSON.parse(fs.readFileSync(path.join(outDirDev, `${devPrefix}-dynamodb.template.json`), 'utf-8'));
      const prodDdb = JSON.parse(fs.readFileSync(path.join(outDirProd, `${prodPrefix}-dynamodb.template.json`), 'utf-8'));
      const devProtections = tableDeletionProtection({}, devPrefix, devDdb);
      const prodProtections = tableDeletionProtection({}, prodPrefix, prodDdb);
      expect(devProtections.every((p) => p === false)).toBe(true);
      expect(prodProtections.some((p) => p === true)).toBe(true);

      // And the CONTAINER's ENVIRONMENT is 'production' in BOTH, regardless.
      const devEcs = JSON.parse(fs.readFileSync(path.join(outDirDev, `${devPrefix}-ecs.template.json`), 'utf-8'));
      const prodEcs = JSON.parse(fs.readFileSync(path.join(outDirProd, `${prodPrefix}-ecs.template.json`), 'utf-8'));
      expect(backendContainerEnv(devEcs).ENVIRONMENT).toBe('production');
      expect(backendContainerEnv(prodEcs).ENVIRONMENT).toBe('production');
    } finally {
      fs.rmSync(outDirDev, { recursive: true, force: true });
      fs.rmSync(outDirProd, { recursive: true, force: true });
    }
  }, 180_000);
});

describe('STRATOCLAVE_API_ENDPOINT is always set on the ECS task, in both entry modes', () => {
  // The claim "the CDK app always sets it" (CHANGELOG.md), evidenced here
  // rather than left as a bare assertion -- `bin/iac.ts`'s `entryUrl`
  // composition feeds BOTH `STRATOCLAVE_API_ENDPOINT` and `CORS_ORIGINS`
  // unconditionally, in every entry mode; this drives the real app and
  // checks the resulting template rather than reading that source by eye.
  test('cloudfront entry: present, and a CloudFront-shaped reference', () => {
    const prefix = `apiendpcf${Math.random().toString(36).slice(2, 6)}`;
    const template = synth(prefix, {});
    const env = backendContainerEnv(template);
    expect(env.STRATOCLAVE_API_ENDPOINT).toBeDefined();
    const serialized = JSON.stringify(env.STRATOCLAVE_API_ENDPOINT);
    expect(serialized).toContain('https://');
    expect(serialized.toLowerCase()).toContain('frontend');
  }, 120_000);

  test('private entry: present, and the operator\'s own PrivateLink domain', () => {
    const prefix = `apiendppriv${Math.random().toString(36).slice(2, 6)}`;
    const domain = 'stratoclave.internal.example.com';
    const template = synth(prefix, {
      STRATOCLAVE_ENTRY: 'private',
      STRATOCLAVE_PRIVATE_CERTIFICATE_ARN:
        'arn:aws:acm:us-east-1:123456789012:certificate/11111111-2222-3333-4444-555555555555',
      STRATOCLAVE_PRIVATE_DOMAIN: domain,
      STRATOCLAVE_PRIVATE_ALLOWED_PRINCIPALS: 'arn:aws:iam::123456789012:root',
    });
    const env = backendContainerEnv(template);
    expect(env.STRATOCLAVE_API_ENDPOINT).toBe(`https://${domain}`);
  }, 120_000);
});

describe('STRATOCLAVE_STS_AUDIENCES / STRATOCLAVE_STS_AUDIENCE_REQUIRED survive a cdk deploy', () => {
  // Both are read by the backend (`mvp.sso_sts.expected_audiences`,
  // `.audience_required`) but were not wired through `bin/iac.ts` at all: a
  // value an operator set by hand on the task definition during the runbook
  // in docs/DEPLOYMENT.md ("Binding AWS sign-in to this gateway") was
  // silently dropped on the next `cdk deploy`, same defect class as the
  // first-admin-email regression `documented-env-wiring.test.ts` exists for.
  test('an exported value reaches the container unchanged', () => {
    const prefix = `stsenv${Math.random().toString(36).slice(2, 6)}`;
    const audiences = 'https://stratoclave.internal.example.com,https://alias.example.com';
    const template = synth(prefix, {
      STRATOCLAVE_STS_AUDIENCES: audiences,
      STRATOCLAVE_STS_AUDIENCE_REQUIRED: 'true',
    });
    const env = backendContainerEnv(template);
    expect(env.STRATOCLAVE_STS_AUDIENCES).toBe(audiences);
    expect(env.STRATOCLAVE_STS_AUDIENCE_REQUIRED).toBe('true');
  }, 120_000);

  test('the keys exist (as empty strings) even when nothing was exported', () => {
    // Same reasoning as `documented-env-wiring.test.ts`'s
    // `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` check: the key SET must not depend
    // on which shell ran the deploy, and the backend already treats an
    // empty string as "not configured" (`core.deployment`'s `.strip()`
    // check), so this is not a behaviour change for an operator who sets
    // neither.
    const prefix = `stsenvoff${Math.random().toString(36).slice(2, 6)}`;
    const template = synth(prefix, {});
    const env = backendContainerEnv(template);
    expect(env).toHaveProperty('STRATOCLAVE_STS_AUDIENCES');
    expect(env).toHaveProperty('STRATOCLAVE_STS_AUDIENCE_REQUIRED');
    expect(env.STRATOCLAVE_STS_AUDIENCES).toBe('');
    expect(env.STRATOCLAVE_STS_AUDIENCE_REQUIRED).toBe('');
  }, 120_000);
});
