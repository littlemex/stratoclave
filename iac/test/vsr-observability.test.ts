import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { VsrObservability } from '../lib/vsr-observability';

/**
 * Telemetry unification — VIEW layer. Proves the dark-ship contract:
 *  - VSR namespace present  => one dashboard with gateway + VSR widgets AND the
 *    three VSR alarms;
 *  - VSR namespace absent    => a gateway-only dashboard and ZERO VSR alarms
 *    (so a missing scrape target can never make an alarm flap).
 */
function synth(vsrMetricNamespace?: string): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'ObsStack', {
    env: { account: '123456789012', region: 'us-west-2' },
  });
  new VsrObservability(stack, 'Obs', {
    prefix: 'stratoclave',
    vsrMetricNamespace,
    vsrMetricsPort: 9190,
  });
  return Template.fromStack(stack);
}

describe('VsrObservability', () => {
  describe('VSR deployed (namespace present)', () => {
    const t = synth('stratoclave/VSR');

    test('one unified dashboard is created', () => {
      t.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
      t.hasResourceProperties('AWS::CloudWatch::Dashboard', {
        DashboardName: 'stratoclave-observability',
      });
    });

    test('the dashboard body references BOTH namespaces (one pane)', () => {
      const dashes = t.findResources('AWS::CloudWatch::Dashboard');
      const body = JSON.stringify(Object.values(dashes)[0]);
      expect(body).toContain('stratoclave/CreditLedger');
      expect(body).toContain('stratoclave/VSR');
    });

    test('the three VSR alarms are created', () => {
      // 3 VSR alarms exactly (errors, telemetry-gap, cache-hit-rate).
      t.resourceCountIs('AWS::CloudWatch::Alarm', 3);
      for (const name of [
        'stratoclave-vsr-errors',
        'stratoclave-vsr-telemetry-gap',
        'stratoclave-vsr-cache-hit-rate-low',
      ]) {
        t.hasResourceProperties('AWS::CloudWatch::Alarm', { AlarmName: name });
      }
    });

    test('telemetry-gap alarm treats MISSING data as breaching (silent loss)', () => {
      t.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: 'stratoclave-vsr-telemetry-gap',
        TreatMissingData: 'breaching',
        ComparisonOperator: 'LessThanThreshold',
      });
    });

    test('error alarm does NOT flap on missing data', () => {
      t.hasResourceProperties('AWS::CloudWatch::Alarm', {
        AlarmName: 'stratoclave-vsr-errors',
        TreatMissingData: 'notBreaching',
      });
    });
  });

  describe('VSR off (no namespace) — dark-ship no-op', () => {
    const t = synth(undefined);

    test('dashboard still exists (gateway half) but references only the ledger ns', () => {
      t.resourceCountIs('AWS::CloudWatch::Dashboard', 1);
      const dashes = t.findResources('AWS::CloudWatch::Dashboard');
      const body = JSON.stringify(Object.values(dashes)[0]);
      expect(body).toContain('stratoclave/CreditLedger');
      expect(body).not.toContain('stratoclave/VSR');
    });

    test('ZERO VSR alarms exist (no target => nothing to flap)', () => {
      t.resourceCountIs('AWS::CloudWatch::Alarm', 0);
    });
  });
});
