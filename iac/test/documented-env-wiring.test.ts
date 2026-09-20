import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

/**
 * A deploy procedure that tells an operator to `export FOO=...` before
 * `cdk deploy`, against a CDK app that never reads `FOO`, is a procedure that
 * cannot work -- and it fails in the worst possible way, because every step
 * reports success. The deploy completes, the rollout reaches COMPLETED, and
 * whatever `FOO` was supposed to switch on simply never happens.
 *
 * This is not hypothetical. `STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL` is the variable
 * that mints the first administrator; `docs/DEPLOYMENT.md` and
 * `iac/scripts/deploy-all.sh`'s own "Next steps" banner both told operators to
 * export it and redeploy the ECS stack, and the CDK app did not contain the
 * string. Following the documented procedure on a fresh prefix produced a
 * deployment with zero users and no error anywhere, and every deployment that
 * already had an administrator was incapable of reproducing it.
 *
 * WHAT THIS FILE ENFORCES
 *
 * Three checks, deliberately different in kind:
 *
 *   1. (the class) Every variable a documented procedure tells the operator to
 *      set for a deploy is READ by the CDK app -- matched as an environment
 *      lookup (`process.env.NAME`, `env.NAME`, either with brackets), never as
 *      a bare occurrence of the name. A comment that mentions a variable must
 *      not be able to satisfy this check, or deleting the wiring while leaving
 *      the comment behind stays green, which is this exact defect again.
 *   2. (the escape hatch) Every entry in `CONSUMED_ELSEWHERE` still earns its
 *      place: it is not read by the app after all (the entry's own claim), and
 *      some procedure really does ask for it. Without these, an allowlist is
 *      just the place where a failing check goes to be silenced.
 *   3. (the instance) The first-admin variable reach the running container
 *      with the value the operator exported. Check 1 would pass on a variable
 *      that is read and then dropped on the floor, which is the same silent
 *      failure one layer in.
 *
 * Checks 1 and 2 are the general net; check 3 is the reproduce/verify pair for
 * the defect above. None of them replaces
 * `dynamodb-table-env-wiring.test.ts` (a declared table nothing wires) or
 * `scheduled-job-env-wiring.test.ts` (a handler reading a var its own Lambda
 * does not declare): those start from a resource and from code. This file
 * starts from what the documentation promised, which is the third side of that
 * triangle and the only one that can see a wire that was never there.
 */
describe('documented env wiring: an exported variable the CDK app never reads', () => {
  const repoRoot = path.resolve(__dirname, '..', '..');
  const iacDir = path.resolve(__dirname, '..');

  /**
   * Documents and scripts that instruct an operator to set variables for a
   * deploy. `every file that instructs a deploy is listed here` is itself
   * checked below, so this list cannot quietly fall behind the docs.
   */
  const PROCEDURES = [
    'docs/DEPLOYMENT.md',
    'docs/ADMIN_GUIDE.md',
    'iac/scripts/deploy-all.sh',
    'iac/scripts/deploy.sh',
    'iac/scripts/build-and-push.sh',
    'scripts/install-infra.sh',
    'README.md',
  ];

  /**
   * Files that mention both a deploy command and an `export` but are not
   * operator instructions, with the reason. Keeping them here rather than
   * silently out of scope is what makes the coverage check above meaningful.
   */
  const NOT_A_PROCEDURE: Record<string, string> = {
    'iac/scripts/post-deploy-validation.sh':
      'exports variables for its own use (AWS_REGION default, CONFIG_S3_URL) rather than telling an operator to set anything',
  };

  /**
   * Variables an operator sets that the CDK app is NOT expected to read, each
   * naming who reads it instead. This list is the whole escape hatch, which is
   * why both of its own properties are asserted rather than trusted.
   */
  const CONSUMED_ELSEWHERE: Record<string, string> = {
    AWS_PROFILE: 'the AWS SDK credential chain',
    AWS_REGION: 'the AWS SDK and CLI; the app takes its region from STRATOCLAVE_REGION / CDK_DEFAULT_REGION',
    AWS_DEFAULT_REGION: 'the AWS SDK and CLI',
    PATH: 'the shell, when a document shows how to put the CLI on it',
    STRATOCLAVE_API_ENDPOINT: 'the stratoclave CLI, which talks to a deployment rather than building one',
  };

  /** Files that could plausibly carry a deploy procedure. */
  function candidateProcedureFiles(): string[] {
    const globs = [
      ...fs.readdirSync(path.join(repoRoot, 'docs')).filter((f) => f.endsWith('.md')).map((f) => `docs/${f}`),
      ...fs.readdirSync(path.join(repoRoot, 'scripts')).filter((f) => f.endsWith('.sh')).map((f) => `scripts/${f}`),
      ...fs.readdirSync(path.join(repoRoot, 'iac', 'scripts')).filter((f) => f.endsWith('.sh')).map((f) => `iac/scripts/${f}`),
      'README.md',
    ];
    return globs.sort();
  }

  function readTsRecursively(dir: string): string {
    let blob = '';
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) blob += readTsRecursively(full);
      else if (entry.name.endsWith('.ts')) blob += fs.readFileSync(full, 'utf-8');
    }
    return blob;
  }

  const cdkAppSource = readTsRecursively(path.join(iacDir, 'bin')) + readTsRecursively(path.join(iacDir, 'lib'));

  /**
   * Whether the app reads this variable out of an environment. Both spellings
   * are real here: `bin/iac.ts` reads `process.env.X`, and
   * `lib/region-config.ts` takes the environment as a parameter and reads
   * `env.X` off it, which is what makes it unit-testable without mutating the
   * process. A bare mention of the name matches neither.
   */
  function isReadByCdkApp(name: string): boolean {
    const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp(`(?:process\\.env|env)(?:\\.${escaped}\\b|\\[['"]${escaped}['"]\\])`).test(cdkAppSource);
  }

  /**
   * Variables a file tells the operator to set for a deploy, in either form
   * documents actually use: `export NAME=value` on its own line (including
   * inside a banner a script echoes), and `NAME=value npx cdk deploy ...`
   * prefixed onto the command.
   */
  function instructedVariables(relPath: string): string[] {
    const text = fs.readFileSync(path.join(repoRoot, relPath), 'utf-8');
    const names = new Set<string>();
    for (const m of text.matchAll(/export ([A-Z][A-Z0-9_]*)=/g)) names.add(m[1]);
    for (const m of text.matchAll(/(?:^|\s)([A-Z][A-Z0-9_]{2,})=\S*\s+(?:npx\s+)?cdk\s/gm)) names.add(m[1]);
    return [...names].sort();
  }

  function instructsADeploy(relPath: string): boolean {
    const text = fs.readFileSync(path.join(repoRoot, relPath), 'utf-8');
    return /cdk (?:deploy|bootstrap)|deploy-all\.sh/.test(text) && /export [A-Z][A-Z0-9_]*=/.test(text);
  }

  test('the harness reads a CDK app and the procedures it claims to (a check on the check)', () => {
    // A wrong path would otherwise make every assertion below vacuous.
    expect(isReadByCdkApp('STRATOCLAVE_PREFIX')).toBe(true);
    expect(isReadByCdkApp('STRATOCLAVE_REGION')).toBe(true); // the `env.X` spelling
    expect(isReadByCdkApp('NO_SUCH_VARIABLE_ANYWHERE')).toBe(false);
    for (const procedure of PROCEDURES) {
      expect(fs.existsSync(path.join(repoRoot, procedure))).toBe(true);
    }
    expect(PROCEDURES.flatMap(instructedVariables).length).toBeGreaterThan(5);
  });

  test('the read predicate ignores prose and prefixes', () => {
    // The two ways a bare-substring match would have lied. Both are live
    // hazards in this repo: its IaC comments name these variables constantly,
    // and `IMAGE_TAG` / `LAMBDA_IMAGE_TAG`, `ALLOW_ADMIN_CREATION` /
    // `ALLOW_ADMIN_CREATION_UNTIL` are real pairs where one is a prefix of the
    // other. The predicate is exercised against a fixture rather than the real
    // source so that it keeps testing the predicate even while the source is
    // mid-change.
    const fixture = [
      '// FOO_MENTIONED_IN_PROSE is read at synth time and written into the task.',
      'const tag = process.env.LAMBDA_IMAGE_TAG || "latest";',
      'const region = env.STRATOCLAVE_REGION;',
      'const other = process.env["QUOTED_LOOKUP"];',
    ].join('\n');
    const reads = (name: string) => {
      const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      return new RegExp(`(?:process\\.env|env)(?:\\.${escaped}\\b|\\[['"]${escaped}['"]\\])`).test(fixture);
    };
    expect(reads('FOO_MENTIONED_IN_PROSE')).toBe(false); // a comment is not a read
    expect(reads('IMAGE_TAG')).toBe(false); // LAMBDA_IMAGE_TAG must not satisfy it
    expect(reads('LAMBDA_IMAGE_TAG')).toBe(true);
    expect(reads('STRATOCLAVE_REGION')).toBe(true); // the injected-env spelling
    expect(reads('QUOTED_LOOKUP')).toBe(true);
  });

  test.each(PROCEDURES)('%s: every variable it tells the operator to set is read by the CDK app', (procedure) => {
    const unread = instructedVariables(procedure).filter(
      (name) => !(name in CONSUMED_ELSEWHERE) && !isReadByCdkApp(name),
    );
    // The remedy is never "delete the assertion": either the app should read
    // the variable, or the procedure should stop asking for it, or it belongs
    // in CONSUMED_ELSEWHERE naming whoever does read it.
    expect(unread).toEqual([]);
  });

  test('every file that instructs a deploy is a listed procedure or a named exception', () => {
    // Otherwise the list above is an invariant asserted only in a comment,
    // which is the same shape of defect this file exists to catch.
    const unaccounted = candidateProcedureFiles().filter(
      (rel) => instructsADeploy(rel) && !PROCEDURES.includes(rel) && !(rel in NOT_A_PROCEDURE),
    );
    expect(unaccounted).toEqual([]);
  });

  test('every CONSUMED_ELSEWHERE entry is still true, and still needed', () => {
    const contradicted = Object.keys(CONSUMED_ELSEWHERE).filter(isReadByCdkApp);
    // An entry that says "something else reads this" while the app reads it is
    // an exemption covering a variable that is no longer exempt.
    expect(contradicted).toEqual([]);

    const instructedAnywhere = new Set(PROCEDURES.flatMap(instructedVariables));
    const unused = Object.keys(CONSUMED_ELSEWHERE).filter((name) => !instructedAnywhere.has(name));
    // And an entry no procedure asks for is an indulgence nobody needs, which
    // is how an allowlist turns into a place to put things.
    expect(unused).toEqual([]);
  });

  describe('the first-admin variable reaches the container', () => {
    const PREFIX = 'docenvwiretest';
    const EMAIL_SENTINEL = 'first-admin@documented-env-wiring.invalid';

    type CfnTemplate = { Resources?: Record<string, any> };

    function synthEcs(extraEnv: Record<string, string>): CfnTemplate {
      const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'stratoclave-doc-env-wiring-'));
      try {
        execFileSync('npx', ['cdk', 'synth', '--all', '-o', outDir, '--quiet'], {
          cwd: iacDir,
          env: {
            ...process.env,
            CDK_DEFAULT_ACCOUNT: '123456789012',
            CDK_DEFAULT_REGION: 'us-east-1',
            STRATOCLAVE_REGION: 'us-east-1',
            STRATOCLAVE_PREFIX: PREFIX,
            IMAGE_TAG: 'doc-env-wiring-test',
            // This test is about env var WIRING, not security posture --
            // nag-synth.test.ts already owns that check on a real synth.
            CDK_NAG: 'off',
            ...extraEnv,
          },
          encoding: 'utf-8',
          stdio: 'pipe',
        });
        return JSON.parse(fs.readFileSync(path.join(outDir, `${PREFIX}-ecs.template.json`), 'utf-8'));
      } finally {
        fs.rmSync(outDir, { recursive: true, force: true });
      }
    }

    /**
     * The environment of the container that runs the backend, by name. Merging
     * every container's environment would pass if these variables were handed
     * to a future sidecar instead, and the seed runs in the backend process or
     * nowhere. Duplicate keys are rejected rather than collapsed, because a
     * duplicate is ECS taking the last one while a reader of the template sees
     * the first.
     */
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

    let exported: Record<string, unknown>;
    let notExported: Record<string, unknown>;

    beforeAll(() => {
      exported = backendContainerEnv(
        synthEcs({ STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL: EMAIL_SENTINEL }),
      );
      notExported = backendContainerEnv(
        synthEcs({ STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL: '' }),
      );
    }, 300_000);

    test('the exported email is the value the container gets', () => {
      // `backend/bootstrap/seed.py::seed_bootstrap_admin` reads this at startup
      // and mints the first admin. Anything short of the exact value here means
      // the documented procedure does nothing.
      expect(exported.STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL).toBe(EMAIL_SENTINEL);
    });

    test('the keys exist even when nothing was exported, so the key set does not depend on the deployer', () => {
      // Two operators deploying the same commit from different shells get the
      // same task definition shape. It also keeps the assertion above honest: a
      // key that appears only when it is set cannot be checked for absence of
      // wiring, which is the defect this file exists for.
      //
      // SCOPE: this property is claimed for these two variables, not for the
      // container environment as a whole -- the block in `bin/iac.ts` still
      // mixes always-pass with pass-only-when-set, because flipping the rest
      // would need a per-variable audit of what the backend does with an empty
      // string versus an absent one. If that audit ever happens, this test
      // generalises to the whole key set and this note goes away.
      expect(notExported).toHaveProperty('STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL');
      // Empty is what the backend already treats as "not set" (it strips the
      // value and tests it for truth), so an un-exported deploy seeds nothing.
      expect(notExported.STRATOCLAVE_BOOTSTRAP_ADMIN_EMAIL).toBe('');
    });
  });
});
