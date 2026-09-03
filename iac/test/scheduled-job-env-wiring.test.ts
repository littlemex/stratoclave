import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * Closes the class of bug this repository has now hit TWICE: every table-name
 * resolver in the backend is `os.getenv("DYNAMODB_<X>_TABLE", "stratoclave-<x>")`
 * (or the `dynamo/client.py::table_name(env_var, fallback)` wrapper around the
 * same call). An unset var is never an error -- it silently repoints the table
 * at a DIFFERENT deployment's table named `stratoclave-<x>`. The ECS service
 * task (`bin/iac.ts`'s `ecsStack`) passes every table env var explicitly and
 * never hits this; a scheduled-Lambda stack that hand-picks a SUBSET of tables
 * for its handler can miss one -- `quota-reconciler-stack.ts` did
 * (`DYNAMODB_QUOTA_EVENTS_TABLE`, found on a real `AccessDeniedException` and
 * already fixed) and `certificate-scheduler-stack.ts` did too
 * (`DYNAMODB_TENANTS_TABLE`, and a SECOND one this very audit found while it
 * was being written: `DYNAMODB_USAGE_LOGS_TABLE`, read unconditionally by
 * `savings_certificate` -> `vsr_reconcile.reconcile_day` -> `_query_usage_day`,
 * with no IAM grant on that table at all before this change).
 *
 * The two halves of this bug have always lived on opposite sides of a
 * language boundary and neither side's existing tests look at the other:
 * the backend's pytest fixtures set EVERY table env var through one shared
 * fixture (so a handler missing one env var in a REAL deploy never fails a
 * backend test), and this repo's OWN CDK unit tests
 * (`quota-reconciler-stack.test.ts` et al.) assert what a stack passes
 * without ever asking what the handler's code actually reads. This test is
 * the seam: it runs a REAL `cdk synth --all` (the same mechanism
 * `nag-synth.test.ts` uses, for a different check) to get what each of the
 * four scheduled-Lambda stacks ACTUALLY declares on its Lambda's environment,
 * and shells out to a REAL static analysis of the REAL backend source
 * (`backend/scripts/scheduled_lambda_env_audit.py`) to get what that
 * handler's own call graph can actually cause a `DYNAMODB_*` env var to be
 * read for. Declared must be a superset of reachable, or the SAME silent
 * fallback that paged an operator on `quota-reconciler` is live again,
 * waiting for the branch of code that reads it to actually run.
 *
 * The audit script documents its own blind spots (dynamic `getattr`,
 * `importlib.import_module` with a non-literal name, decorator-based
 * registries other than the one named `register_check` pattern it already
 * special-cases) -- see its module docstring. This test does not re-litigate
 * those; it asserts what the script COULD determine, and a reviewer checking
 * whether a NEW scheduled Lambda is covered should read that docstring, not
 * assume this test's green run means every code path was seen.
 */
describe('scheduled-Lambda env wiring: handler reachability vs. what the stack declares', () => {
  const iacDir = path.resolve(__dirname, '..');
  const backendDir = path.resolve(__dirname, '../../backend');
  const auditScript = path.join(backendDir, 'scripts', 'scheduled_lambda_env_audit.py');
  const PREFIX = 'envwiretest';

  type FunctionEnv = { functionName: string; env: Record<string, string> };

  function synthLambdaEnvironments(): Record<string, Record<string, string>> {
    const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-env-wiring-synth-'));
    try {
      execFileSync(
        'npx',
        [
          'cdk', 'synth', '--all', '-o', outDir, '--quiet',
          '-c', 'quotaReconciler=true',
          '-c', 'quotaGrants=true',
          '-c', 'ledgerProjector=true',
          '-c', 'certificateScheduler=true',
        ],
        {
          cwd: iacDir,
          env: {
            ...process.env,
            CDK_DEFAULT_ACCOUNT: '123456789012',
            CDK_DEFAULT_REGION: 'us-east-1',
            STRATOCLAVE_REGION: 'us-east-1',
            STRATOCLAVE_PREFIX: PREFIX,
            LAMBDA_IMAGE_TAG: 'lambda-env-wiring-test',
            // This test is about env var WIRING, not security posture --
            // nag-synth.test.ts already owns that check on a real synth.
            CDK_NAG: 'off',
          },
          encoding: 'utf-8',
          stdio: 'pipe',
        },
      );

      const functions: Record<string, Record<string, string>> = {};
      for (const file of fs.readdirSync(outDir)) {
        if (!file.endsWith('.template.json')) continue;
        const template = JSON.parse(fs.readFileSync(path.join(outDir, file), 'utf-8'));
        const resources = template.Resources || {};
        for (const resource of Object.values(resources) as any[]) {
          if (resource.Type !== 'AWS::Lambda::Function') continue;
          const functionName = resource.Properties?.FunctionName;
          const variables = resource.Properties?.Environment?.Variables || {};
          if (typeof functionName === 'string') {
            functions[functionName] = variables;
          }
        }
      }
      return functions;
    } finally {
      fs.rmSync(outDir, { recursive: true, force: true });
    }
  }

  function reachableDynamoDbEnvVars(module: string, functionNames: string[]): {
    required: string[];
    unresolved: string[];
  } {
    const out = execFileSync(
      'python3',
      [auditScript, module, ...functionNames],
      { cwd: backendDir, encoding: 'utf-8' },
    );
    const parsed = JSON.parse(out);
    return { required: parsed.dynamodb_env_vars, unresolved: parsed.unresolved };
  }

  let lambdaEnvironments: Record<string, Record<string, string>>;

  beforeAll(() => {
    lambdaEnvironments = synthLambdaEnvironments();
  }, 120_000);

  test('the synth actually produced all six scheduled-Lambda functions (a sanity check on the harness itself)', () => {
    const expectedFunctionNames = [
      `${PREFIX}-certificate-issuer`,
      `${PREFIX}-ledger-projector`,
      `${PREFIX}-ledger-reconciler`,
      `${PREFIX}-quota-reconciler`,
      `${PREFIX}-quota-period-rollover`,
      `${PREFIX}-quota-grant-sweeper`,
    ];
    for (const name of expectedFunctionNames) {
      expect(lambdaEnvironments).toHaveProperty(name);
    }
  });

  test.each([
    ['certificate-issuer', 'mvp.learning.certificate_scheduler', ['handler']],
    ['ledger-projector', 'billing.ledger_projector', ['handler']],
    ['ledger-reconciler', 'billing.ledger_reconciler', ['handler']],
    ['quota-reconciler', 'mvp.observability.quota_reconciler', ['handler']],
    ['quota-period-rollover', 'mvp.observability.quota_reconciler', ['rollover_handler']],
    ['quota-grant-sweeper', 'mvp.grants', ['sweep_handler']],
  ] as [string, string, string[]][])(
    '%s: every DYNAMODB_* env var the handler (%s) can reach is set on the deployed Lambda',
    (fnSuffix, module, functionNames) => {
      const functionName = `${PREFIX}-${fnSuffix}`;
      const declared = lambdaEnvironments[functionName];
      expect(declared).toBeDefined();

      const { required } = reachableDynamoDbEnvVars(module, functionNames);
      // The interesting assertion: reachable must be a SUBSET of declared. A
      // failure here means the handler's own code can execute a branch that
      // reads a DYNAMODB_*_TABLE env var this stack never sets -- which falls
      // through to that repository's hardcoded `stratoclave-<x>` default, a
      // DIFFERENT deployment's table, exactly like the two real bugs above.
      const missing = required.filter((envVar) => !(envVar in declared));
      expect(missing).toEqual([]);
    },
    30_000,
  );
});
