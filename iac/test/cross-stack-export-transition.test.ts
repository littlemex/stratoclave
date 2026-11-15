import {
  checkExportTransition,
  isSafeTransition,
  exportNames,
  allImports,
  type StackSet,
} from './_cross-stack-export-transition';

/**
 * Pure unit tests of the checker itself, against hand-made CloudFormation
 * JSON — no `cdk synth`, no AWS calls. These exist specifically to PROVE the
 * checker catches the shape of bug round-1 implementation review's B1
 * blocker was: going straight from `vpc-switch` to `vpc` had the producer
 * stack (AlbStack) remove an export in the SAME transition where a consumer
 * stack (FrontendStack) ALSO stopped importing it — safe-looking from only
 * the NEW state's point of view, but exactly what CloudFormation refuses,
 * because it checks the removed export against what is CURRENTLY deployed
 * (the OLD state), not against the consumer's not-yet-applied update.
 */
describe('checkExportTransition (pure, hand-made templates)', () => {
  function producerWithExport(exportName: string): StackSet['alb'] {
    return {
      Resources: {
        Alb1234: { Type: 'AWS::ElasticLoadBalancingV2::LoadBalancer', Properties: {} },
      },
      Outputs: {
        AlbDnsName: {
          Value: { 'Fn::GetAtt': ['Alb1234', 'DNSName'] },
          Export: { Name: exportName },
        },
      },
    };
  }

  function producerWithoutExport(): StackSet['alb'] {
    return {
      Resources: {},
      Outputs: {},
    };
  }

  function consumerImporting(exportName: string): StackSet['frontend'] {
    return {
      Resources: {
        Distribution1: {
          Type: 'AWS::CloudFront::Distribution',
          Properties: {
            Origins: [{ DomainName: { 'Fn::ImportValue': exportName } }],
          },
        },
      },
      Outputs: {},
    };
  }

  function consumerNotImporting(): StackSet['frontend'] {
    return {
      Resources: {
        Distribution1: {
          Type: 'AWS::CloudFront::Distribution',
          Properties: { Origins: [] },
        },
      },
      Outputs: {},
    };
  }

  const EXPORT = 'stratoclave-alb:ExportsOutputFnGetAttAlb1234DNSName1A2B3C4D';

  test('THE REGRESSION THIS FILE EXISTS TO PIN: a direct vpc-switch -> vpc jump (producer removes the export, consumer ALSO stops importing it, in the SAME transition) is flagged UNSAFE', () => {
    // This is exactly the pre-vpc-contract design: one step takes the
    // producer from declaring the export to not declaring it, AND the
    // consumer from importing it to not importing it, at the same time.
    // Looking only at the NEW state (producer silent, consumer silent) it
    // looks perfectly consistent -- which is why a test that only
    // synthesizes each state ALONE (the review's own description of what
    // the pre-existing test file did) cannot see the problem at all. The
    // checker must look at what the OLD state's consumer was STILL doing
    // at the moment the producer's export disappears.
    const oldStacks: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerImporting(EXPORT) };
    const newStacks: StackSet = { alb: producerWithoutExport(), frontend: consumerNotImporting() };

    const problems = checkExportTransition(oldStacks, newStacks);
    expect(problems.unsafeRemovals).toContain(EXPORT);
    expect(isSafeTransition(problems)).toBe(false);
  });

  test('the vpc-contract-mediated two-step sequence is flagged SAFE at each step', () => {
    // Step 1 (vpc-switch -> vpc-contract): producer keeps declaring the
    // export UNCHANGED; only the consumer stops importing it.
    const switchState: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerImporting(EXPORT) };
    const contractState: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerNotImporting() };
    const step1 = checkExportTransition(switchState, contractState);
    expect(isSafeTransition(step1)).toBe(true);

    // Step 2 (vpc-contract -> vpc): NOW the producer removes the export --
    // safely, because nothing in the OLD (vpc-contract) state imports it
    // any more (step 1 already took care of that).
    const vpcState: StackSet = { alb: producerWithoutExport(), frontend: consumerNotImporting() };
    const step2 = checkExportTransition(contractState, vpcState);
    expect(isSafeTransition(step2)).toBe(true);
  });

  test('a NEW state importing an export nothing in the NEW state declares is flagged, independent of any OLD state', () => {
    const oldStacks: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerImporting(EXPORT) };
    // A hypothetical broken NEW state: the consumer still imports the
    // export, but the producer forgot to keep declaring it (a different
    // bug shape from the OLD-state-still-importing one above -- this one
    // is visible from the NEW state alone).
    const brokenNewStacks: StackSet = { alb: producerWithoutExport(), frontend: consumerImporting(EXPORT) };
    const problems = checkExportTransition(oldStacks, brokenNewStacks);
    expect(problems.unresolvedImports).toContain(EXPORT);
    expect(isSafeTransition(problems)).toBe(false);
  });

  test('an export that never changes across the transition is never flagged', () => {
    const a: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerImporting(EXPORT) };
    const b: StackSet = { alb: producerWithExport(EXPORT), frontend: consumerImporting(EXPORT) };
    expect(isSafeTransition(checkExportTransition(a, b))).toBe(true);
  });

  test('exportNames/allImports ignore non-string Export.Name / Fn::ImportValue values rather than crashing', () => {
    const stacks: StackSet = {
      alb: {
        Outputs: {
          Weird: { Export: { Name: { 'Fn::Sub': 'not-a-plain-string' } } },
        },
        Resources: {
          R: { Properties: { 'Fn::ImportValue': { 'Fn::Sub': 'also-not-a-plain-string' } } },
        },
      },
    };
    expect(exportNames(stacks).size).toBe(0);
    expect(allImports(stacks)).toEqual([]);
  });
});
