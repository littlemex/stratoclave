import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * A table declared in the DynamoDB stack and never wired into the running
 * service task falls through to `table_name`'s hardcoded
 * `stratoclave-<x>` fallback in every prefixed environment -- the store
 * points at a table that does not exist, or at a DIFFERENT deployment's
 * table of the same shape. This has already happened twice on the
 * scheduled-Lambda side of this app (see `scheduled-job-env-wiring.test.ts`,
 * which guards that a handler's own reachable `DYNAMODB_*` env vars are a
 * subset of what its Lambda declares), and it is a distinct failure mode
 * from the one that test catches: this one is "a table exists that nothing
 * ever wires anywhere", not "a handler reads a var its own function does not
 * declare". Neither reachability analysis nor a per-handler subset check
 * would catch a table added to `DynamoDBStack` with no corresponding line in
 * the main service task's `environment` block in `bin/iac.ts` -- there is
 * no handler code to read it and fail loudly; the gap is silent until an
 * operator notices the store using the wrong table.
 *
 * This test is the other direction: for every table `DynamoDBStack` (the
 * table declarations, `lib/dynamodb-stack.ts`) actually synthesizes, the
 * main service task's container environment (`bin/iac.ts`'s `environment`
 * block, synthesized into the ECS `TaskDefinition`) must reference it.
 * `DynamoDBStack` and `EcsStack` are different CDK stacks, so a value like
 * `dynamoDBStack.someTable.tableName` used in the ECS stack's environment
 * synthesizes as `{ "Fn::ImportValue": "...ExportsOutputRef<TableLogicalId><hash>" }`
 * rather than a literal string -- the table's own CloudFormation logical id
 * is embedded verbatim inside that import name, so matching on it (rather
 * than on the literal `${prefix}-x` table name, which never appears
 * cross-stack at all) is what makes this check work without CloudFormation
 * having resolved anything.
 */
describe('DynamoDB table env wiring: every declared table reaches the service task', () => {
  const iacDir = path.resolve(__dirname, '..');
  const PREFIX = 'ddbenvwiretest';

  type CfnTemplate = { Resources?: Record<string, any> };

  function synthDefaultStacks(): { dynamodb: CfnTemplate; ecs: CfnTemplate } {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-ddb-env-wiring-synth-'));
    try {
      execFileSync(
        'npx',
        ['cdk', 'synth', '--all', '-o', outDir, '--quiet'],
        {
          cwd: iacDir,
          env: {
            ...process.env,
            CDK_DEFAULT_ACCOUNT: '123456789012',
            CDK_DEFAULT_REGION: 'us-east-1',
            STRATOCLAVE_REGION: 'us-east-1',
            STRATOCLAVE_PREFIX: PREFIX,
            IMAGE_TAG: 'ddb-env-wiring-test',
            // This test is about env var WIRING, not security posture --
            // nag-synth.test.ts already owns that check on a real synth.
            CDK_NAG: 'off',
          },
          encoding: 'utf-8',
          stdio: 'pipe',
        },
      );

      const read = (fileName: string): CfnTemplate =>
        JSON.parse(fs.readFileSync(path.join(outDir, fileName), 'utf-8'));

      return {
        dynamodb: read(`${PREFIX}-dynamodb.template.json`),
        ecs: read(`${PREFIX}-ecs.template.json`),
      };
    } finally {
      fs.rmSync(outDir, { recursive: true, force: true });
    }
  }

  function tableLogicalIdsAndNames(template: CfnTemplate): Record<string, string> {
    const tables: Record<string, string> = {};
    for (const [logicalId, resource] of Object.entries(template.Resources || {})) {
      if (resource.Type === 'AWS::DynamoDB::Table') {
        tables[logicalId] = resource.Properties?.TableName;
      }
    }
    return tables;
  }

  function serviceTaskEnvironmentBlob(template: CfnTemplate): string {
    const taskDefs = Object.values(template.Resources || {}).filter(
      (r: any) => r.Type === 'AWS::ECS::TaskDefinition',
    );
    expect(taskDefs).toHaveLength(1);
    const containers = (taskDefs[0] as any).Properties?.ContainerDefinitions || [];
    expect(containers.length).toBeGreaterThan(0);
    // Stringify the whole Environment array of every container rather than
    // picking one container by name: the assertion this test makes --
    // "this table's logical id appears SOMEWHERE in what the task
    // declares" -- does not depend on which container reads it, and a
    // future sidecar container is covered for free.
    return JSON.stringify(containers.map((c: any) => c.Environment || []));
  }

  let dynamodb: CfnTemplate;
  let ecs: CfnTemplate;
  let tables: Record<string, string>;
  let ecsEnvironmentBlob: string;

  beforeAll(() => {
    ({ dynamodb, ecs } = synthDefaultStacks());
    tables = tableLogicalIdsAndNames(dynamodb);
    ecsEnvironmentBlob = serviceTaskEnvironmentBlob(ecs);
  }, 120_000);

  test('the synth actually produced tables and a service task (a sanity check on the harness itself)', () => {
    expect(Object.keys(tables).length).toBeGreaterThanOrEqual(25);
    expect(ecsEnvironmentBlob.length).toBeGreaterThan(0);
    // Every existing table-name env var follows this prefix; if that stops
    // being true the substring match below could pass for the wrong reason.
    expect(ecsEnvironmentBlob).toContain('DYNAMODB_');
  });

  test('every table the DynamoDB stack declares is referenced somewhere in the service task environment', () => {
    const missing = Object.entries(tables)
      .filter(([logicalId]) => !ecsEnvironmentBlob.includes(logicalId))
      .map(([logicalId, tableName]) => `${tableName} (${logicalId})`);
    // A failure here means a table exists that no `DYNAMODB_*_TABLE` entry
    // in `bin/iac.ts` ever passes to the running task -- the exact gap that
    // left the promotion-candidates table pointing at its own hardcoded
    // fallback name until this test existed.
    expect(missing).toEqual([]);
  });
});
