import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { Template } from 'aws-cdk-lib/assertions';
import { EcsStack } from '../lib/ecs-stack';

/**
 * M16 (docs/design/price-feeds.md, price-feeds contract): the operator selects the
 * live price source at deploy time through `STRATOCLAVE_PRICE_SOURCE`, which
 * `iac/bin/iac.ts` reads and passes to `EcsStack` as the `priceSource` prop.
 *
 * Interface (CONTRACT.md, resolved): with the source unset, the synthesised
 * template must be byte-for-byte what it was before this change -- no
 * `STRATOCLAVE_PRICE_SOURCE` environment entry, no `AllowReadOnlyPriceDiscovery`
 * IAM statement granting `pricing:GetProducts`. With the source selected, both
 * must appear. `bedrock:ListFoundationModelAgreementOffers` is a SEPARATE case
 * (#281 item 2): it is granted unconditionally regardless of `priceSource`, since
 * the probe's own assertion 0 needs it on every promotion attempt whether or not
 * the live price-feed subsystem this file is otherwise about is turned on. The
 * three feed knobs
 * (`STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS`, `STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS`,
 * `STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS`) are part of what "the task
 * definition carries" alongside the source: present on the task definition when
 * supplied, absent when not.
 *
 * `priceSource` and `priceFeed` are the two dedicated named props CONTRACT.md's
 * Interface section names for this surface:
 *
 *     priceSource?: string
 *     priceFeed?: { intervalSeconds?: number; budgetSeconds?: number; staleAfterSeconds?: number }
 *
 * `bin/iac.ts` reads `STRATOCLAVE_PRICE_SOURCE` and the three
 * `STRATOCLAVE_PRICE_FEED_*_SECONDS` vars from the deploy environment under
 * those same names and passes them through as these props -- so driving the
 * knobs via `priceFeed` (rather than the generic `environment` passthrough
 * every other var in this suite uses) exercises the actual path a deployment
 * takes, not a mechanism this change did not add.
 */
function synth(opts: {
  priceSource?: string;
  priceFeed?: { intervalSeconds?: number; budgetSeconds?: number; staleAfterSeconds?: number };
} = {}): Template {
  const app = new cdk.App();
  const net = new cdk.Stack(app, 'Net', { env: { account: '123456789012', region: 'us-west-2' } });
  const vpc = new ec2.Vpc(net, 'Vpc', { maxAzs: 2, natGateways: 1 });
  const sg = new ec2.SecurityGroup(net, 'Sg', { vpc, description: 'x' });
  const repo = ecr.Repository.fromRepositoryName(net, 'Repo', 'stratoclave-backend');
  const alb = new elbv2.ApplicationLoadBalancer(net, 'Alb', { vpc, internetFacing: true });
  const tg = new elbv2.ApplicationTargetGroup(net, 'Tg', {
    vpc, port: 8000, protocol: elbv2.ApplicationProtocol.HTTP, targetType: elbv2.TargetType.IP,
  });
  const id = `EcsPriceFeeds${opts.priceSource ? 'With' : 'Without'}Source${opts.priceFeed ? 'Knobs' : 'NoKnobs'}`;
  const stack = new EcsStack(app, id, {
    env: { account: '123456789012', region: 'us-west-2' },
    prefix: 'stratoclave',
    vpc,
    securityGroup: sg,
    repository: repo,
    targetGroup: tg,
    userPoolArn: 'arn:aws:cognito-idp:us-west-2:123456789012:userpool/us-west-2_p',
    dynamoDbTableArns: ['arn:aws:dynamodb:us-west-2:123456789012:table/stratoclave-users'],
    environment: { DATABASE_TYPE: 'dynamodb' },
    ...(opts.priceSource ? { priceSource: opts.priceSource } : {}),
    ...(opts.priceFeed ? { priceFeed: opts.priceFeed } : {}),
  });
  return Template.fromStack(stack);
}

/** True if any IAM policy statement in the synthesized template grants `action`. */
function hasAction(template: Template, action: string): boolean {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies).some((policy: any) => {
    const statements = policy.Properties?.PolicyDocument?.Statement ?? [];
    return statements.some((statement: any) => {
      const actions: string[] = Array.isArray(statement.Action)
        ? statement.Action
        : [statement.Action];
      return actions.includes(action);
    });
  });
}

/** True if any IAM policy statement in the synthesized template grants the gated
 * Price List read (`pricing:GetProducts`) — the half of the former price-discovery
 * pair that is still gated on `priceSource`. `bedrock:ListFoundationModelAgreementOffers`
 * moved to the unconditional `AllowBedrockReadOnly` statement (#281 item 2: the
 * probe's own assertion 0 needs it regardless of the live price-feed source), and
 * is checked separately, below, in both scenarios. */
function hasPriceDiscoveryStatement(template: Template): boolean {
  return hasAction(template, 'pricing:GetProducts');
}

/** The value of `envName` on the (single) backend container, or `undefined` if not
 * set on any task definition in the template. */
function containerEnvValue(template: Template, envName: string): string | undefined {
  const taskDefs = template.findResources('AWS::ECS::TaskDefinition');
  for (const def of Object.values(taskDefs) as any[]) {
    const containers = def.Properties?.ContainerDefinitions ?? [];
    for (const container of containers) {
      const match = (container.Environment ?? []).find((env: any) => env.Name === envName);
      if (match) return match.Value;
    }
  }
  return undefined;
}

function hasEnvVar(template: Template, envName: string): boolean {
  return containerEnvValue(template, envName) !== undefined;
}

const FEED_KNOBS = {
  intervalSeconds: 300,
  budgetSeconds: 15,
  staleAfterSeconds: 3600,
};

const FEED_KNOB_VAR_BY_PROP = [
  ['intervalSeconds', 'STRATOCLAVE_PRICE_FEED_INTERVAL_SECONDS'],
  ['budgetSeconds', 'STRATOCLAVE_PRICE_FEED_BUDGET_SECONDS'],
  ['staleAfterSeconds', 'STRATOCLAVE_PRICE_FEED_STALE_AFTER_SECONDS'],
] as const;

describe('EcsStack price-feeds deployment (M16)', () => {
  describe('source selected', () => {
    const template = synth({ priceSource: 'bedrock-live', priceFeed: FEED_KNOBS });

    test('the task definition carries STRATOCLAVE_PRICE_SOURCE', () => {
      expect(containerEnvValue(template, 'STRATOCLAVE_PRICE_SOURCE')).toBe('bedrock-live');
    });

    test('the price-discovery IAM statement is attached', () => {
      expect(hasPriceDiscoveryStatement(template)).toBe(true);
    });

    test.each(FEED_KNOB_VAR_BY_PROP)(
      'the task definition carries %s as %s with the supplied value',
      (prop, envName) => {
        expect(containerEnvValue(template, envName)).toBe(String(FEED_KNOBS[prop]));
      },
    );
  });

  describe('source not selected', () => {
    const template = synth();

    test('no STRATOCLAVE_PRICE_SOURCE on the task definition', () => {
      expect(hasEnvVar(template, 'STRATOCLAVE_PRICE_SOURCE')).toBe(false);
    });

    test('no price-discovery IAM statement is attached', () => {
      expect(hasPriceDiscoveryStatement(template)).toBe(false);
    });

    test.each(FEED_KNOB_VAR_BY_PROP)('no %s (%s) on the task definition when priceFeed is omitted',
      (_prop, envName) => {
        expect(hasEnvVar(template, envName)).toBe(false);
      },
    );
  });

  test.each([
    ['with the source selected', { priceSource: 'bedrock-live' }],
    ['without the source selected', {}],
  ] as const)(
    'the price-discovery IAM statement and STRATOCLAVE_PRICE_SOURCE are attached together or not at all (%s)',
    (_label, opts) => {
      const template = synth(opts);
      expect(hasEnvVar(template, 'STRATOCLAVE_PRICE_SOURCE')).toBe(
        hasPriceDiscoveryStatement(template),
      );
    },
  );

  // #281 item 2: the probe's assertion 0 reads `bedrock:ListFoundationModelAgreementOffers`
  // on every promotion attempt, independent of the live price-feed subsystem this file
  // otherwise tests. A deployment with no `priceSource` configured must still carry this
  // permission, or every probe fails closed for a reason that has nothing to do with the
  // binding being checked.
  test.each([
    ['with the source selected', { priceSource: 'bedrock-live' }],
    ['without the source selected', {}],
  ] as const)(
    'bedrock:ListFoundationModelAgreementOffers is granted unconditionally (%s)',
    (_label, opts) => {
      const template = synth(opts);
      expect(hasAction(template, 'bedrock:ListFoundationModelAgreementOffers')).toBe(true);
    },
  );
});
