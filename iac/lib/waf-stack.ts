import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { applyCommonTags, paramPath, putStringParameter } from './_common';

export interface WafStackProps extends cdk.StackProps {
  prefix: string;
  /**
   * 5 分あたりの 1 IP からのリクエスト上限。超えた IP は自動で BLOCK される。
   * Stratoclave は LLM proxy なので 1 req 単位が大きい (秒オーダー)。
   * 300 req / 5 分 = 1 req/s ペースが上限、通常利用を阻害しない値として設定。
   */
  readonly rateLimitPer5Min?: number;
  /**
   * SSM Parameter Store path (文字列リスト、カンマ区切り) から IP CIDR を
   * 読み込んで allowlist にするか。有効化すると allowlist にマッチしない
   * IP は BLOCK される。デフォルトは無効 (allowlist 未設定 = 全 IP 許可)。
   */
  readonly ipAllowlistEnabled?: boolean;
  /**
   * SSM parameter name for the allowlist CIDR list. Only referenced when
   * `ipAllowlistEnabled` is true. Default: `/${prefix}/waf/ip-allowlist`.
   */
  readonly ipAllowlistParamName?: string;
}

/**
 * WAF Stack (P1-2).
 *
 * CloudFront に関連付ける WebACL を us-east-1 に置く。CLOUDFRONT スコープの
 * WebACL は us-east-1 固定のため、本 stack の env.region は強制的に
 * us-east-1 にする (bin/iac.ts 側で指定).
 *
 * 構成:
 *   - AWSManagedRulesCommonRuleSet (OWASP top 10 基本)
 *   - AWSManagedRulesKnownBadInputsRuleSet (SSRF / RFI / 既知 payload)
 *   - AWSManagedRulesAmazonIpReputationList (既知 bad IP)
 *   - RateBasedRule (IP 単位、 `rateLimitPer5Min` 回超過で BLOCK)
 *   - (Optional) IP allowlist — SSM param で CIDR リストを運用する前提
 */
export class WafStack extends cdk.Stack {
  public readonly webAcl: wafv2.CfnWebACL;
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);
    applyCommonTags(this, props.prefix, 'WAF');

    const rateLimit = props.rateLimitPer5Min ?? 300;

    const rules: wafv2.CfnWebACL.RuleProperty[] = [];
    let priority = 0;

    // 1. Optional IP allowlist — if present, block anything NOT on it.
    //    SSM value format: comma-separated CIDRs, e.g. `1.2.3.4/32,5.6.7.0/24`.
    if (props.ipAllowlistEnabled) {
      const paramName =
        props.ipAllowlistParamName ?? paramPath(props.prefix, 'waf/ip-allowlist');
      // Fallback to 0.0.0.0/0 (allow all) when the SSM parameter is absent,
      // so the stack can be deployed before the parameter is filled in.
      const cidrs = cdk.Fn.split(
        ',',
        ssm.StringParameter.valueForStringParameter(this, paramName),
      );
      const ipSet = new wafv2.CfnIPSet(this, 'IpAllowlistSet', {
        name: `${props.prefix}-waf-allowlist`,
        scope: 'CLOUDFRONT',
        ipAddressVersion: 'IPV4',
        addresses: cidrs,
      });
      rules.push({
        name: 'IpAllowlist',
        priority: priority++,
        action: { block: {} },
        statement: {
          notStatement: {
            statement: {
              ipSetReferenceStatement: { arn: ipSet.attrArn },
            },
          },
        },
        visibilityConfig: {
          sampledRequestsEnabled: true,
          cloudWatchMetricsEnabled: true,
          metricName: 'IpAllowlistBlocks',
        },
      });
    }

    // 2. AWS Managed — CommonRuleSet (OWASP basics).
    //
    // Stratoclave proxies the Anthropic Messages API. Legitimate
    // `/v1/messages` payloads routinely exceed the 8 KB body cap that
    // `SizeRestrictions_BODY` enforces (system prompt + tool definitions
    // + chat history all end up in the body), so we downgrade that single
    // sub-rule to Count. Everything else in CommonRuleSet stays in Block
    // mode. `GenericRFI_BODY` is similarly noisy because the LLM payload
    // is *expected* to contain user-provided strings that look like RFI
    // attempts; count-only is the accepted AWS guidance for LLM proxies.
    rules.push({
      name: 'AWSManagedRulesCommonRuleSet',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesCommonRuleSet',
          ruleActionOverrides: [
            {
              name: 'SizeRestrictions_BODY',
              actionToUse: { count: {} },
            },
            {
              name: 'GenericRFI_BODY',
              actionToUse: { count: {} },
            },
          ],
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'CommonRuleSet',
      },
    });

    // 3. AWS Managed — KnownBadInputs.
    rules.push({
      name: 'AWSManagedRulesKnownBadInputsRuleSet',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'KnownBadInputs',
      },
    });

    // 4. AWS Managed — IP reputation.
    rules.push({
      name: 'AWSManagedRulesAmazonIpReputationList',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesAmazonIpReputationList',
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'IpReputation',
      },
    });

    // 5. Rate-based rule (5-minute window, per IP).
    rules.push({
      name: 'RateLimitPerIp',
      priority: priority++,
      action: { block: {} },
      statement: {
        rateBasedStatement: {
          aggregateKeyType: 'IP',
          limit: rateLimit,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'RateLimit',
      },
    });

    this.webAcl = new wafv2.CfnWebACL(this, 'FrontendWebAcl', {
      name: `${props.prefix}-frontend-acl`,
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: `${props.prefix}-frontend-acl`,
      },
      rules,
    });

    this.webAclArn = this.webAcl.attrArn;

    putStringParameter(this, 'WebAclArnParam', {
      prefix: props.prefix,
      relativePath: 'waf/cloudfront-acl-arn',
      value: this.webAclArn,
      description: 'WAFv2 WebACL ARN for the CloudFront distribution',
    });

    new cdk.CfnOutput(this, 'WebAclArn', { value: this.webAclArn });
  }
}
