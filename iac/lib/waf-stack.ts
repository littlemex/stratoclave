import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { applyCommonTags, paramPath, putStringParameter } from './_common';

export interface WafStackProps extends cdk.StackProps {
  prefix: string;
  /**
   * Maximum AUTHENTICATED requests per IP per 5-minute window.
   *
   * This used to be 300 for all traffic, on the reasoning that an LLM request
   * takes seconds so 1 req/s per IP could not impede normal usage. Measured on
   * 2026-08-24 that is false for the clients this gateway exists to serve: a
   * concurrency sweep from one host had 1018 of 1024 requests blocked with 403
   * while the service itself was serving 60 req/s comfortably. An aggregator in
   * front of the gateway — a semantic router, a benchmark harness, a CI fleet
   * behind one NAT — is one IP carrying many users' traffic.
   *
   * A per-IP rate rule is the wrong instrument for that traffic, and the gateway
   * already has the right one: per-user token quotas and per-tenant dollar pools,
   * which bound cost per identity rather than per address. So this ceiling is set
   * from the concurrency target, high enough that legitimate traffic at the target
   * never meets it, and its remaining job is to bound a pathological flood.
   *
   * Unauthenticated traffic keeps a tight limit of its own — see
   * `unauthenticatedRateLimitPer5Min`.
   */
  readonly rateLimitPer5Min?: number;
  /**
   * Maximum UNAUTHENTICATED requests per IP per 5-minute window.
   *
   * Requests arriving without a non-empty `Authorization` header cannot be
   * attributed to a user, so no token quota bounds them and the IP is the only
   * key available. This keeps the original tight ceiling for exactly that
   * traffic.
   */
  readonly unauthenticatedRateLimitPer5Min?: number;
  /**
   * Whether to read IP CIDRs from an SSM Parameter Store path (string list, comma-separated)
   * and use them as an allowlist. When enabled, IPs not on the allowlist are BLOCKed.
   * Disabled by default (no allowlist configured = all IPs allowed).
   */
  readonly ipAllowlistEnabled?: boolean;
  /**
   * SSM parameter name for the allowlist CIDR list. Only referenced when
   * `ipAllowlistEnabled` is true. Default: `/${prefix}/waf/ip-allowlist`.
   */
  readonly ipAllowlistParamName?: string;
  /**
   * URI prefixes that carry LLM traffic rather than browser traffic. Managed
   * rules are applied differently on either side of this line — see
   * `KNOWN_BAD_INPUTS_BODY_RULES_COUNTED`. Defaults to the Messages route (`/v1/`)
   * and the OpenAI Responses route (`/openai/`).
   */
  readonly dataPlanePathPrefixes?: string[];
}

/** Default URI prefixes treated as the LLM data plane. */
const DEFAULT_DATA_PLANE_PREFIXES = ['/v1/', '/openai/'];

/**
 * Managed sub-rules that inspect the REQUEST BODY for injection patterns, run in
 * Count mode on the data plane only.
 *
 * An agent's request body legitimately contains relative paths
 * (`../../lib/foo.ts`), HTML and JavaScript, shell commands, and cloud metadata
 * endpoints, because those are the files and questions the user is working on.
 * Every one of those trips a body-scoped injection rule, and the caller sees
 * CloudFront's generic 403 page with nothing to suggest that WAF, not the
 * gateway, said no.
 *
 * Measured against a deployed distribution with an identical key, path, and
 * headers (the account was out of credit, so "reached the application" shows up
 * as HTTP 402):
 *
 *   body "hi"                        -> 402  (reached the app)
 *   body "cat ../../etc/passwd"      -> 403  (GenericLFI_BODY)
 *   body "<script>alert(1)</script>" -> 403  (CrossSiteScripting_BODY)
 *   body "' OR 1=1 --"               -> 402  (no SQLi group enabled)
 *
 * A real coding session dies on its first turn: the agent's system prompt embeds
 * the project's own file paths.
 *
 * Body inspection also protects nothing on this route. These rules exist for an
 * application that interpolates the body into HTML, a filesystem path, or a
 * shell; Stratoclave forwards the body to Bedrock as an opaque prompt, so a
 * `<script>` tag is data and never executes. Count (rather than removal) keeps
 * the CloudWatch metric, so the signal survives without the false positives.
 *
 * The relaxation is deliberately confined to the data plane. The console and the
 * management API are browser-facing, where markup in a body *is* dangerous, so
 * they keep the managed groups at full strength.
 *
 * Be honest about what the data plane gives up. Because CommonRuleSet is not
 * applied there at all, that route also loses the group's non-body checks —
 * `GenericLFI_QUERYARGUMENTS`, `CrossSiteScripting_URIPATH`,
 * `SizeRestrictions_QUERYSTRING`, `NoUserAgent_HEADER`, and the rest. WAF applies
 * sub-rule overrides per group rather than per path, so keeping the group with
 * only its body rules counted requires a second instance, and that costs another
 * 700 WCU against a default 1500 WCU WebACL budget. What remains on the data
 * plane: KnownBadInputs (bad methods, host-header abuse, known-bad URIs), the IP
 * reputation list, the rate limit, and the application's own contract — JSON on a
 * handful of paths, every request carrying a scoped `sk-stratoclave-*` key.
 *
 * Rule names are verbatim from the group, not guessed. AWS silently ignores an
 * override that names a rule the group does not have, so a typo would leave the
 * data plane blocking while every test still passed. Verify with:
 *
 *   aws wafv2 describe-managed-rule-group --vendor-name AWS \
 *     --name AWSManagedRulesKnownBadInputsRuleSet --scope CLOUDFRONT \
 *     --region us-east-1 --query 'Rules[].Name'
 *
 * CommonRuleSet needs no override list here because it is not applied to the
 * data plane at all (its `SizeRestrictions_BODY`, `GenericLFI_BODY`,
 * `GenericRFI_BODY`, `CrossSiteScripting_BODY`, and `EC2MetaDataSSRF_BODY` are
 * the rules the measurements above tripped). KnownBadInputs *is* applied there,
 * for its method / host / URI checks, so its two body-content sub-rules are the
 * ones that need downgrading.
 */
const KNOWN_BAD_INPUTS_BODY_RULES_COUNTED = [
  // `${jndi:...}` and Java gadget strings show up in security-related prompts.
  'Log4JRCE_BODY',
  'JavaDeserializationRCE_BODY',
  // React/JS payload shapes appear whenever the user is working on a frontend.
  'ReactJSRCE_BODY',
] as const;

/**
 * Matches a request whose URI path is on the data plane.
 *
 * Two details matter more than they look:
 *
 * The path is decoded and normalised before matching. Without that,
 * `/openai/../api/admin/...` and `%2e%2e` variants classify as data plane —
 * skipping CommonRuleSet — while anything downstream that folds dot segments
 * still routes them at the management API. `URL_DECODE` then `NORMALIZE_PATH` is
 * the standard pairing for that.
 *
 * A regex rather than a byte match, because `searchString` is a blob: the API
 * takes it base64-encoded, so a plain string passed through the CLI or an SDK is
 * decoded into three junk bytes and the rule matches nothing — silently, since a
 * scope-down that never matches simply widens the rule (measured against a live
 * distribution). `regexString` has no such ambiguity.
 */
function uriOnDataPlane(prefixes: string[]): wafv2.CfnWebACL.StatementProperty {
  if (prefixes.length === 0) {
    throw new Error('dataPlanePathPrefixes must not be empty');
  }
  const alternatives = prefixes
    .map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
    .join('|');
  return {
    regexMatchStatement: {
      fieldToMatch: { uriPath: {} },
      regexString: `^(${alternatives})`,
      textTransformations: [
        { priority: 0, type: 'URL_DECODE' },
        { priority: 1, type: 'NORMALIZE_PATH' },
      ],
    },
  };
}

/**
 * WAF Stack (P1-2).
 *
 * Places the WebACL associated with CloudFront in us-east-1. CLOUDFRONT-scoped
 * WebACLs are fixed to us-east-1, so env.region for this stack is forced to
 * us-east-1 (set in bin/iac.ts).
 *
 * Configuration:
 *   - AWSManagedRulesCommonRuleSet (OWASP top 10 basics)
 *   - AWSManagedRulesKnownBadInputsRuleSet (SSRF / RFI / known bad payloads)
 *   - AWSManagedRulesAmazonIpReputationList (known bad IPs)
 *   - RateBasedRule x2 (per IP; a high ceiling for authenticated traffic, a tight
 *     one for requests with no usable `Authorization` header)
 *   - (Optional) IP allowlist — driven by a CIDR list stored in an SSM parameter
 */
export class WafStack extends cdk.Stack {
  public readonly webAcl: wafv2.CfnWebACL;
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: WafStackProps) {
    super(scope, id, props);
    applyCommonTags(this, props.prefix, 'WAF');

    const rateLimit = props.rateLimitPer5Min ?? 300;
    const unauthenticatedRateLimit = props.unauthenticatedRateLimitPer5Min ?? 300;

    // Requests that carry a non-empty Authorization header. Used as a scope-down
    // on both rate rules so the two classes of traffic are counted separately:
    // authenticated traffic is bounded per identity by token quotas and only
    // loosely per IP, while unattributable traffic has nothing but the IP.
    const isAuthenticated: wafv2.CfnWebACL.StatementProperty = {
      sizeConstraintStatement: {
        // `singleHeader` is typed `any` in the L1 construct, so it is passed to
        // CloudFormation verbatim and has to use CloudFormation's casing. Written
        // in camelCase it synthesises cleanly and is rejected at deploy.
        fieldToMatch: { singleHeader: { Name: 'authorization' } },
        comparisonOperator: 'GT',
        size: 0,
        textTransformations: [{ priority: 0, type: 'NONE' }],
      },
    };

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

    // Browser traffic vs LLM traffic. The console and management API are
    // browser-facing and keep the managed groups at full strength; the data
    // plane runs the body-injection sub-rules in Count mode (see
    // KNOWN_BAD_INPUTS_BODY_RULES_COUNTED for the measurements behind that).
    const dataPlanePrefixes =
      props.dataPlanePathPrefixes ?? DEFAULT_DATA_PLANE_PREFIXES;
    const onDataPlane = uriOnDataPlane(dataPlanePrefixes);
    const offDataPlane: wafv2.CfnWebACL.StatementProperty = {
      notStatement: { statement: onDataPlane },
    };

    // 2. AWS Managed — CommonRuleSet (OWASP basics), console / management API.
    //    Not applied to the data plane: its body sub-rules are the false-positive
    //    source, and a second full instance would push the WebACL past the
    //    default 1500 WCU budget (this group alone costs 700).
    rules.push({
      name: 'AWSManagedRulesCommonRuleSet',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesCommonRuleSet',
          scopeDownStatement: offDataPlane,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'CommonRuleSet',
      },
    });

    // 3. AWS Managed — KnownBadInputs, console / management API (full strength).
    rules.push({
      name: 'AWSManagedRulesKnownBadInputsRuleSet',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          scopeDownStatement: offDataPlane,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'KnownBadInputs',
      },
    });

    // 4. AWS Managed — KnownBadInputs on the data plane, with the body-content
    //    sub-rules counted. Keeps the non-body protections (bad methods, host
    //    header abuse, known-bad URIs) blocking for `/v1/*` and `/openai/*`
    //    while letting a prompt contain whatever the user is working on.
    rules.push({
      name: 'KnownBadInputsDataPlane',
      priority: priority++,
      overrideAction: { none: {} },
      statement: {
        managedRuleGroupStatement: {
          vendorName: 'AWS',
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          scopeDownStatement: onDataPlane,
          ruleActionOverrides: KNOWN_BAD_INPUTS_BODY_RULES_COUNTED.map((name) => ({
            name,
            actionToUse: { count: {} },
          })),
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'KnownBadInputsDataPlane',
      },
    });

    // 5. AWS Managed — IP reputation.
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

    // 6. Rate-based rules (5-minute window, per IP), one per class of traffic.
    // Authenticated traffic first: a high ceiling whose job is to bound a flood,
    // because cost is already bounded per identity by the token quota.
    rules.push({
      name: 'RateLimitPerIp',
      priority: priority++,
      action: { block: {} },
      statement: {
        rateBasedStatement: {
          aggregateKeyType: 'IP',
          limit: rateLimit,
          scopeDownStatement: isAuthenticated,
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'RateLimit',
      },
    });

    // Unattributable traffic keeps the tight ceiling: with no user to charge, the
    // address is the only key, so this is the only thing bounding it. Last rule in
    // the chain — no further `priority++` is needed after this one.
    rules.push({
      name: 'RateLimitPerIpUnauthenticated',
      priority: priority,
      action: { block: {} },
      statement: {
        rateBasedStatement: {
          aggregateKeyType: 'IP',
          limit: unauthenticatedRateLimit,
          scopeDownStatement: { notStatement: { statement: isAuthenticated } },
        },
      },
      visibilityConfig: {
        sampledRequestsEnabled: true,
        cloudWatchMetricsEnabled: true,
        metricName: 'RateLimitUnauthenticated',
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
