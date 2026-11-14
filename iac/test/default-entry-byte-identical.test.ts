import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * `STRATOCLAVE_ENTRY` unset (or `cloudfront`) must synthesize the same
 * CloudFormation templates as before this feature existed. That one-time
 * before/after proof was run once, by hand, with `cdk synth --all` against
 * the pre-change and post-change trees (the `.template.json` files were
 * byte-identical; only `.metadata.json` trace line numbers moved, because
 * the source files grew lines — construct traces are not part of what gets
 * deployed). That proof cannot be re-run by a future CI job because there is
 * no "before" tree once this commit lands.
 *
 * What CAN be re-run forever is this: snapshot the stacks THIS feature
 * actually branches on (network, alb, cognito — the ones whose construction
 * changed shape when entry-mode support was added) plus the two ECS task-env
 * entries that changed (`CORS_ORIGINS`, `STRATOCLAVE_API_ENDPOINT`), and fail
 * the moment a future change to entry-config.ts, network-stack.ts,
 * alb-stack.ts, or bin/iac.ts's own branching moves the default path's
 * output by so much as one byte. The full nine-stack ECR/DynamoDB/WAF/
 * Frontend templates are NOT snapshotted here: this feature's branching does
 * not touch them at all in `cloudfront` mode (see bin/iac.ts — ecr,
 * dynamodb, waf and frontend are constructed identically regardless of
 * entry mode when that mode is `cloudfront`), so snapshotting them would
 * only add churn this test would need to absorb for unrelated changes.
 *
 * Synth is run with the ambient AWS credentials NEUTRALISED (no AWS_PROFILE,
 * fake AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, AWS_EC2_METADATA_DISABLED).
 * Without this, the `cdk` CLI resolves its own "default account" from
 * whatever real credentials happen to be ambient in the shell that runs the
 * test and OVERWRITES `CDK_DEFAULT_ACCOUNT` with that real account before
 * spawning the app subprocess — regardless of what this file passes in its
 * own `env` object. Measured directly: with ambient credentials left alone,
 * the synthesized templates contained a real AWS account id, baked into
 * literal `Account` properties, ARNs, and bucket names, and that id reached
 * a committed snapshot once already. Neutralising credentials makes the
 * `cdk` CLI unable to resolve anything beyond what this file explicitly
 * passes, so `CDK_DEFAULT_ACCOUNT=123456789012` is the value every
 * downstream `this.account` resolves to.
 */
describe('STRATOCLAVE_ENTRY unset (default): template output is pinned, stack-for-stack', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'defaultentrytest';
  const DUMMY_ACCOUNT = '123456789012';

  // Exactly the nine stacks a default (no context flags) deploy has always
  // produced. A ratchet: if entry-mode branching ever added or removed one
  // of these for the DEFAULT mode, this list — and the stack-list assertion
  // below — catches it independently of the per-stack content snapshots.
  const DEFAULT_STACK_IDS = [
    'network',
    'dynamodb',
    'ecr',
    'alb',
    'waf',
    'frontend',
    'cognito',
    'ecs',
    'config',
  ];

  // The subset this feature's branching actually touches in `cloudfront`
  // mode, and the only templates whose full content is snapshotted.
  const SNAPSHOTTED_STACK_IDS = ['network', 'alb', 'cognito'];

  /**
   * A synth-time credential-free environment. See the file doc for why this
   * is load-bearing, not cosmetic: without it, a real AWS account id reaches
   * the output (and, previously, a committed snapshot).
   */
  function hermeticEnv(extra: Record<string, string> = {}): NodeJS.ProcessEnv {
    const env: NodeJS.ProcessEnv = { ...process.env };
    delete env.AWS_PROFILE;
    delete env.AWS_DEFAULT_PROFILE;
    delete env.AWS_SESSION_TOKEN;
    delete env.AWS_REGION;
    delete env.AWS_DEFAULT_REGION;
    delete env.AWS_CONTAINER_CREDENTIALS_RELATIVE_URI;
    delete env.AWS_CONTAINER_CREDENTIALS_FULL_URI;
    delete env.AWS_WEB_IDENTITY_TOKEN_FILE;
    delete env.STRATOCLAVE_ENTRY;
    return {
      ...env,
      AWS_ACCESS_KEY_ID: 'AKIAFAKEFAKEFAKEFAKE',
      AWS_SECRET_ACCESS_KEY: 'fakefakefakefakefakefakefakefakefakefake',
      AWS_EC2_METADATA_DISABLED: 'true',
      CDK_DEFAULT_ACCOUNT: DUMMY_ACCOUNT,
      CDK_DEFAULT_REGION: 'us-east-1',
      STRATOCLAVE_REGION: 'us-east-1',
      STRATOCLAVE_PREFIX: PREFIX,
      CDK_NAG: 'off',
      ...extra,
    };
  }

  /** Strip the CDK bookkeeping resource that carries no deployed behaviour. */
  function stripCdkMetadata(template: Record<string, any>): Record<string, any> {
    if (!template.Resources?.CDKMetadata) return template;
    const resources = { ...template.Resources };
    delete resources.CDKMetadata;
    return { ...template, Resources: resources };
  }

  /**
   * Replace content that is expected to change for reasons unrelated to
   * this feature (an asset's content hash moves when ANY file bundled into
   * it changes, including ones this feature does not touch) with a fixed
   * placeholder, so the snapshot only breaks on changes that matter to it.
   */
  function normalizeAssetHashes(template: Record<string, any>): Record<string, any> {
    const text = JSON.stringify(template).replace(/[0-9a-f]{64}/g, '<ASSET_HASH>');
    return JSON.parse(text);
  }

  function sanitize(template: Record<string, any>): Record<string, any> {
    return normalizeAssetHashes(stripCdkMetadata(template));
  }

  let outDir: string;
  let rawTemplates: Record<string, Record<string, any>>;

  beforeAll(() => {
    outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-default-entry-'));
    execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
      cwd: iacDir,
      env: hermeticEnv(),
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    rawTemplates = {};
    for (const id of DEFAULT_STACK_IDS) {
      const templatePath = path.join(outDir, `${PREFIX}-${id}.template.json`);
      rawTemplates[id] = JSON.parse(fs.readFileSync(templatePath, 'utf-8'));
    }
  }, 120_000);

  afterAll(() => {
    if (outDir) fs.rmSync(outDir, { recursive: true, force: true });
  });

  test('synthesizes exactly the nine default-mode stacks — no private-link stack, no entry-mode stack list change', () => {
    const produced = fs
      .readdirSync(outDir)
      .filter((f) => f.endsWith('.template.json'))
      .map((f) => f.replace(/\.template\.json$/, ''))
      .sort();
    const expected = DEFAULT_STACK_IDS.map((id) => `${PREFIX}-${id}`).sort();
    expect(produced).toEqual(expected);
  });

  test.each(SNAPSHOTTED_STACK_IDS)('%s stack template is byte-for-byte pinned', (id) => {
    expect(sanitize(rawTemplates[id])).toMatchSnapshot();
  });

  test('the ECS task env entries this feature branches on are pinned', () => {
    const taskDefs = Object.values(rawTemplates.ecs.Resources || {}).filter(
      (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
    );
    expect(taskDefs).toHaveLength(1);
    const containers = (taskDefs[0] as any).Properties.ContainerDefinitions as Array<{
      Name: string;
      Environment: Array<{ Name: string; Value: string }>;
    }>;
    const backend = containers.find((c) => /backend/i.test(c.Name));
    expect(backend).toBeDefined();
    const env: Record<string, string> = {};
    for (const pair of backend!.Environment) env[pair.Name] = pair.Value;
    expect({
      CORS_ORIGINS: env.CORS_ORIGINS,
      STRATOCLAVE_API_ENDPOINT: env.STRATOCLAVE_API_ENDPOINT,
    }).toMatchSnapshot();
  });

  test('no 12-digit number other than the dummy account appears anywhere in any default-mode template', () => {
    const offenders: Record<string, string[]> = {};
    for (const id of DEFAULT_STACK_IDS) {
      const text = JSON.stringify(rawTemplates[id]);
      const found = Array.from(new Set(text.match(/\b\d{12}\b/g) ?? [])).filter(
        (n) => n !== DUMMY_ACCOUNT,
      );
      if (found.length > 0) offenders[id] = found;
    }
    // A failure here means a real AWS account id (or some other unrelated
    // 12-digit number) reached the synthesized output — the exact class of
    // leak this file's hermetic env exists to prevent. See the file doc.
    expect(offenders).toEqual({});
  });
});
