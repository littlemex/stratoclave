import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';
import {
  applyCommonTags,
  putStringParameter,
  ALB_IDLE_TIMEOUT_SECONDS,
} from './_common';
import type { CloudfrontOriginMode } from './entry-config';

export interface AlbStackProps extends cdk.StackProps {
  prefix: string;
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;

  /** true = VPC-only ALB / false = internet-facing @default false */
  internal?: boolean;
  /** health check path @default '/health' */
  healthCheckPath?: string;
  /** target port @default 8000 */
  targetPort?: number;
  /** HTTPS certificate (optional) */
  certificateArn?: string;
  /** Custom domain name for display purposes (optional) */
  domainName?: string;
  /**
   * Enable ALB deletion protection. Production must set this to true so
   * that a `cdk destroy` can't take the Backend offline by accident.
   * @default false
   */
  deletionProtection?: boolean;
  /**
   * Whether to add a port-80 :80-redirect-to-:443 listener. Named for what
   * it does when a certificate IS set (the only case that matters: with no
   * certificate, port 80 is the ONLY listener this stack ever creates —
   * there is no TLS to redirect to — so this prop has no effect there at
   * all, and `false` with no `certificateArn` would silently leave port 80
   * open with no listener disabled, the opposite of its name. Synth refuses
   * that combination outright instead of silently ignoring it; see the
   * constructor.
   *
   * @default true — exactly today's behaviour: a certificate gets a :80
   * redirect-to-:443 listener alongside :443, so `cloudfront` deployments
   * (which do not set a certificate here at all) and any future ALB that
   * *does* want the redirect are unaffected.
   *
   * `STRATOCLAVE_ENTRY=private` passes `false` alongside a `certificateArn`.
   * PrivateLink carries no TLS of its own, so the first byte of a port-80
   * request — including an API key — would already be in cleartext inside
   * the gateway's own VPC before a redirect could happen; no port 80 may be
   * open at all on that path, not even a redirect.
   *
   * Only ever applies to the PUBLIC/PRIVATE-entry ALB this stack builds
   * (`cloudfrontOrigin`'s internal ALB, below, never takes a certificate at
   * all — see that prop's doc).
   */
  httpRedirectListener?: boolean;
  /**
   * `routing.http.xff_header_processing.mode` ALB attribute. Left unset
   * (AWS default: `append`) unless a caller passes one, so `cloudfront`
   * deployments keep today's behaviour exactly.
   *
   * `STRATOCLAVE_ENTRY=private` passes `'remove'`: on that path every
   * request arrives from the internal NLB, never a client directly, so a
   * client-supplied `X-Forwarded-For` has no legitimate content and must not
   * reach the application — removing it rather than appending to it is what
   * makes that true regardless of what a caller sends. See
   * `backend/core/rate_limit.py` for the consequence (the per-IP limiter
   * falls back to `request.client.host`, which is coarse on this path
   * because every request shares the ALB's own address — documented, not
   * fixed, in docs/DEPLOYMENT.md).
   */
  xffHeaderProcessingMode?: 'append' | 'preserve' | 'remove';
  /**
   * Enable ALB access logs to a dedicated, private S3 bucket this stack
   * creates (SSE-S3, `BLOCK_ALL` public access, TLS-enforced, 90-day
   * lifecycle expiry). Left off by default so `cloudfront` deployments keep
   * today's behaviour exactly (no bucket, no attribute, zero template
   * diff): their request-level forensics come from CloudFront's own edge
   * logs, which this ALB never sees traffic without.
   *
   * `STRATOCLAVE_ENTRY=private` passes `true`. On that path there is no
   * CloudFront, no WAF, and (XFF stripped) no client address in the
   * backend's own logs either — the ALB access log is the only per-request,
   * per-connection record this entry mode has at all, so it is not optional
   * the way it is for `cloudfront`.
   * @default false
   */
  enableAccessLogs?: boolean;

  /**
   * `STRATOCLAVE_CLOUDFRONT_ORIGIN` (see `lib/entry-config.ts`). @default 'public'
   *
   * Independent of `internal`/`certificateArn` above, which are the
   * `STRATOCLAVE_ENTRY=private` axis. This axis decides whether — and how
   * many — ALBs this stack builds for the `cloudfront`-entry path:
   *
   *   - `'public'`: exactly today. One ALB, built from `internal`/
   *     `certificateArn`/etc above, construct ids unchanged (`Alb`,
   *     `BackendTargetGroup`, `HttpListener`, `HttpsListener`).
   *   - `'vpc-expand'` / `'vpc-switch'`: the same public ALB as above, PLUS a
   *     second, internal one (construct ids `InternalAlb`,
   *     `InternalBackendTargetGroup`, `InternalHttpListener`; no HTTPS
   *     listener, no certificate, no custom name — CDK-generated, in the
   *     VPC's `PrivateIsolated` subnets, HTTP :80 only). Exposed as
   *     `internalAlb`/`internalTargetGroup`/`additionalTargetGroup` below so
   *     `bin/iac.ts` can register the ECS service with both target groups.
   *   - `'vpc'`: only the internal ALB — the public one, its target group,
   *     its listener, and its SSM parameters are not created at all.
   *
   * Every ALB this stack ever builds, in every mode, goes through the one
   * `buildAlb` private method below, so the tuned values it sets (health
   * check, idle timeout, the two routing attributes) cannot drift between
   * whichever ALBs happen to exist in a given mode.
   */
  cloudfrontOrigin?: CloudfrontOriginMode;

  /**
   * `STRATOCLAVE_ENTRY`. @default 'cloudfront'
   *
   * Needed here (not just in `cloudfrontOrigin` above) for exactly one
   * decision: whether the INTERNAL ALB also gets an HTTPS :443 listener.
   *
   *   - `'cloudfront'` / `'private'`: unchanged from before `'both'`
   *     existed. `'private'`'s one ALB is built on the PRIMARY path
   *     (`internal: true`, `certificateArn` set, `httpRedirectListener:
   *     false`) exactly as today; `'cloudfront'`'s `cloudfrontOrigin`
   *     modes behave as documented on that prop.
   *   - `'both'`: CloudFront (via a VPC origin) and PrivateLink (via the
   *     internal NLB) share ONE internal ALB and one backend. This stack
   *     refuses any `cloudfrontOrigin` other than `'vpc'` in this mode (see
   *     `lib/entry-config.ts::resolveCloudfrontOriginConfig`, which refuses
   *     it first — this is a same-stack assertion of that invariant, not
   *     the primary guard) — a `'both'` deployment with a public ALB
   *     alongside PrivateLink would defeat the whole point of adding
   *     PrivateLink. `certificateArn` (above) is REQUIRED in this mode and
   *     is attached to the INTERNAL ALB's new HTTPS :443 listener (serving
   *     `lib/private-link-stack.ts`'s NLB target group) — not to a primary
   *     ALB, because `'both'` never builds one. The internal ALB's :80
   *     listener stays a plain HTTP forward (never a redirect) regardless:
   *     CloudFront's VPC origin needs an actual response on :80, and
   *     redirecting it to :443 would just make CloudFront retry the
   *     redirect as a new request with no way to reach :443 without TLS
   *     tools CloudFront's custom-origin HTTP path does not have.
   */
  entryMode?: 'cloudfront' | 'private' | 'both';
}

/** What `buildAlb` returns: everything a caller needs to wire the rest of the app to one ALB. */
interface BuiltAlb {
  alb: elbv2.ApplicationLoadBalancer;
  targetGroup: elbv2.ApplicationTargetGroup;
  /**
   * Undefined exactly when `httpBehavior === 'closed'` (private-entry-only,
   * `httpRedirectListener: false`) — honestly typed, unlike the field this
   * replaced, which was declared non-optional while the constructor's
   * private-entry branch left it unassigned in that exact case.
   */
  httpListener?: elbv2.ApplicationListener;
  httpsListener?: elbv2.ApplicationListener;
}

/**
 * What the :80 listener does.
 *   - `'forward'`: plain HTTP forward to the target group. Default when no
 *     `certificateArn` is set. Also what every CloudFront/VPC-origin-facing
 *     ALB uses on :80 regardless of whether it ALSO has a `certificateArn`
 *     (the `'both'` entry mode's shared internal ALB) — CloudFront needs an
 *     actual response on :80, never a redirect.
 *   - `'redirect-to-https'`: today's `STRATOCLAVE_ENTRY=private` default
 *     when a certificate is set and `httpRedirectListener` is not
 *     explicitly `false`.
 *   - `'closed'`: no :80 listener at all — today's `STRATOCLAVE_ENTRY=private`
 *     behaviour when `httpRedirectListener: false` is passed alongside a
 *     certificate.
 */
type HttpBehavior = 'forward' | 'redirect-to-https' | 'closed';

interface BuildAlbOptions {
  /** Construct-id prefix: '' for the primary ALB (today's ids, unchanged), 'Internal' for the second one. */
  idPrefix: string;
  prefix: string;
  vpc: ec2.IVpc;
  securityGroup: ec2.ISecurityGroup;
  vpcSubnets?: ec2.SubnetSelection;
  internal: boolean;
  /** undefined => CDK-generated name. The internal ALB (vpc-expand/vpc-switch/vpc/both) always passes undefined — see `cloudfrontOrigin`'s prop doc. */
  loadBalancerName?: string;
  healthCheckPath: string;
  targetPort: number;
  deletionProtection: boolean;
  certificateArn?: string;
  /** @default certificateArn ? 'redirect-to-https' : 'forward' — i.e. unchanged inference from today's `httpRedirectListener` for the primary ALB. Callers that need something other than that inference (the internal ALB, in every mode that builds one) pass it explicitly. */
  httpBehavior?: HttpBehavior;
  xffHeaderProcessingMode?: 'append' | 'preserve' | 'remove';
  enableAccessLogs?: boolean;
}

export class AlbStack extends cdk.Stack {
  /**
   * The "primary" ALB/target group/listener for whatever this deployment's
   * `cloudfrontOrigin` mode is: the public ALB whenever one exists
   * (`public`/`vpc-expand`/`vpc-switch` — unchanged meaning from before this
   * feature), or the internal ALB when it is the only one (`vpc`, and every
   * `STRATOCLAVE_ENTRY=private` deployment, which never sets `cloudfrontOrigin`
   * to anything but `public` and always means "the one ALB this stack built"
   * by `alb`). `bin/iac.ts`'s existing `albStack.alb.loadBalancerDnsName` /
   * `albStack.targetGroup` call sites keep working unchanged across every mode.
   */
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly targetGroup: elbv2.ApplicationTargetGroup;
  /** See `BuiltAlb.httpListener`'s doc: undefined under `STRATOCLAVE_ENTRY=private` with `httpRedirectListener: false`. */
  public readonly httpListener?: elbv2.ApplicationListener;
  public readonly httpsListener?: elbv2.ApplicationListener;

  /** Defined iff a public ALB exists (`public`/`vpc-expand`/`vpc-switch`). Same object as `alb` in those modes. */
  public readonly publicAlb?: elbv2.ApplicationLoadBalancer;
  /** Defined iff an internal ALB exists (`vpc-expand`/`vpc-switch`/`vpc`, or any `entryMode: 'both'` deployment). */
  public readonly internalAlb?: elbv2.ApplicationLoadBalancer;
  public readonly internalTargetGroup?: elbv2.ApplicationTargetGroup;
  /**
   * The "other" target group, only when BOTH ALBs exist (`vpc-expand`/
   * `vpc-switch`) — i.e. exactly the one `bin/iac.ts` must additionally
   * register the ECS service with, alongside `targetGroup`. Undefined in
   * every mode where only one ALB exists, so a caller cannot accidentally
   * attach to the same target group twice.
   */
  public readonly additionalTargetGroup?: elbv2.ApplicationTargetGroup;

  constructor(scope: Construct, id: string, props: AlbStackProps) {
    super(scope, id, props);

    const { prefix } = props;
    const entryMode = props.entryMode ?? 'cloudfront';
    const cloudfrontOrigin = props.cloudfrontOrigin ?? 'public';
    if (entryMode === 'both' && cloudfrontOrigin !== 'vpc') {
      // Primary guard is `resolveCloudfrontOriginConfig` in
      // lib/entry-config.ts. This throw only fires if a future caller
      // constructs AlbStack directly with an inconsistent combination,
      // bypassing that guard — same posture as NetworkStack's own
      // defence-in-depth assertion.
      throw new Error(
        `AlbStack: entryMode "both" requires cloudfrontOrigin "vpc" (got "${cloudfrontOrigin}"). ` +
          'A "both" deployment keeps exactly one ALB, internal only, serving both CloudFront ' +
          '(via a VPC origin) and PrivateLink; a public ALB alongside PrivateLink would defeat ' +
          'the reason PrivateLink exists. This should have been refused already by ' +
          'lib/entry-config.ts::resolveCloudfrontOriginConfig.',
      );
    }
    const needsPublicAlb =
      entryMode !== 'both' &&
      (cloudfrontOrigin === 'public' ||
        cloudfrontOrigin === 'vpc-expand' ||
        cloudfrontOrigin === 'vpc-switch');
    const needsInternalAlb = entryMode === 'both' || cloudfrontOrigin !== 'public';

    // `httpRedirectListener: false` only means something alongside a
    // certificate: it suppresses the :80-redirect-to-:443 listener that the
    // certificate branch below would otherwise add. With no certificateArn
    // at all, port 80 is the ALB's only listener (plain HTTP forward, no
    // redirect exists to suppress), so `httpRedirectListener: false` with
    // no certificate would silently do nothing — opening port 80 anyway,
    // the opposite of what the prop name promises. Refuse that combination
    // at synth rather than let it look honoured.
    if (props.httpRedirectListener === false && !props.certificateArn) {
      throw new Error(
        'AlbStack: httpRedirectListener: false has no effect without certificateArn — ' +
          'there is no HTTPS listener to redirect to, so port 80 (plain HTTP forward) ' +
          'would stay open regardless, silently ignoring the setting. Pass certificateArn ' +
          'alongside it (as STRATOCLAVE_ENTRY=private does), or drop httpRedirectListener.',
      );
    }

    let publicBuilt: BuiltAlb | undefined;
    if (needsPublicAlb) {
      publicBuilt = this.buildAlb({
        idPrefix: '',
        prefix,
        vpc: props.vpc,
        securityGroup: props.securityGroup,
        internal: !!props.internal,
        loadBalancerName: `${prefix}-alb`,
        healthCheckPath: props.healthCheckPath || '/health',
        targetPort: props.targetPort || 8000,
        deletionProtection: props.deletionProtection ?? false,
        certificateArn: props.certificateArn,
        httpBehavior: props.certificateArn
          ? props.httpRedirectListener === false
            ? 'closed'
            : 'redirect-to-https'
          : 'forward',
        xffHeaderProcessingMode: props.xffHeaderProcessingMode,
        enableAccessLogs: props.enableAccessLogs,
      });

      putStringParameter(this, 'AlbDnsParam', {
        prefix,
        relativePath: 'alb/dns-name',
        value: publicBuilt.alb.loadBalancerDnsName,
        description: 'ALB DNS name',
      });
      putStringParameter(this, 'AlbArnParam', {
        prefix,
        relativePath: 'alb/arn',
        value: publicBuilt.alb.loadBalancerArn,
        description: 'ALB ARN',
      });
      putStringParameter(this, 'AlbTargetGroupArnParam', {
        prefix,
        relativePath: 'alb/target-group-arn',
        value: publicBuilt.targetGroup.targetGroupArn,
        description: 'ALB Target Group ARN',
      });
      new cdk.CfnOutput(this, 'AlbDnsName', { value: publicBuilt.alb.loadBalancerDnsName });
      new cdk.CfnOutput(this, 'AlbArn', { value: publicBuilt.alb.loadBalancerArn });

      this.publicAlb = publicBuilt.alb;
    }

    let internalBuilt: BuiltAlb | undefined;
    if (needsInternalAlb) {
      // `entryMode === 'both'` is the only case the internal ALB ever takes
      // a certificate: it then also serves PrivateLink (via
      // `lib/private-link-stack.ts`'s NLB target group) on :443, alongside
      // CloudFront's VPC origin on :80 — one ALB, two front doors, one
      // backend. Every other mode that builds this ALB (`vpc-expand`/
      // `vpc-switch`/`vpc`) passes no certificate at all: it is never
      // reached directly by a client, only by the CloudFront VPC origin
      // service, HTTP only (see docs/DEPLOYMENT.md, "VPC origin" — no TLS
      // can be issued for `internal-*.elb.amazonaws.com`, so there is no
      // HTTPS listener to build there even optionally).
      const internalCertificateArn = entryMode === 'both' ? props.certificateArn : undefined;
      if (entryMode === 'both' && !internalCertificateArn) {
        throw new Error(
          'AlbStack: entryMode "both" requires certificateArn — the ACM certificate for the ' +
            'shared internal ALB\'s HTTPS :443 listener, which lib/private-link-stack.ts\'s NLB ' +
            'target group forwards to. Same requirement as STRATOCLAVE_ENTRY=private, which ' +
            'resolveEntryConfig/resolvePrivateEntryConfig already enforce before this stack is ' +
            'constructed in "both" mode.',
        );
      }
      internalBuilt = this.buildAlb({
        idPrefix: 'Internal',
        prefix,
        vpc: props.vpc,
        securityGroup: props.securityGroup,
        vpcSubnets: props.vpc.selectSubnets({ subnetType: ec2.SubnetType.PRIVATE_ISOLATED }),
        internal: true,
        loadBalancerName: undefined,
        healthCheckPath: props.healthCheckPath || '/health',
        targetPort: props.targetPort || 8000,
        deletionProtection: props.deletionProtection ?? false,
        certificateArn: internalCertificateArn,
        // Always 'forward' on :80, even when a certificate is also present
        // ('both' mode): see `entryMode`'s prop doc for why a redirect here
        // would break CloudFront's VPC origin.
        httpBehavior: 'forward',
        // Real access logs only for 'both': on every other mode that builds
        // this ALB, CloudFront is the only way in and its own edge logs are
        // the forensic record (same reasoning `AwsSolutions-ELB2`'s blanket
        // suppression in bin/iac.ts gives). 'both' also has a PrivateLink
        // leg on :443 that never touches CloudFront at all — same posture
        // as `STRATOCLAVE_ENTRY=private`'s own `enableAccessLogs`, and
        // bin/iac.ts strips that blanket suppression for this stack in
        // 'both' mode for the same reason it already does for 'private'.
        enableAccessLogs: entryMode === 'both' ? props.enableAccessLogs : undefined,
      });

      putStringParameter(this, 'InternalAlbDnsParam', {
        prefix,
        relativePath: 'alb/internal-dns-name',
        value: internalBuilt.alb.loadBalancerDnsName,
        description: 'Internal ALB DNS name (CloudFront VPC origin target)',
      });
      putStringParameter(this, 'InternalAlbArnParam', {
        prefix,
        relativePath: 'alb/internal-arn',
        value: internalBuilt.alb.loadBalancerArn,
        description: 'Internal ALB ARN',
      });
      new cdk.CfnOutput(this, 'InternalAlbDnsName', {
        value: internalBuilt.alb.loadBalancerDnsName,
      });
      new cdk.CfnOutput(this, 'InternalAlbArn', { value: internalBuilt.alb.loadBalancerArn });

      this.internalAlb = internalBuilt.alb;
      this.internalTargetGroup = internalBuilt.targetGroup;
    }

    // "Primary" = the public ALB whenever one exists (unchanged meaning from
    // before this feature existed), else the internal one. See the field doc.
    const primary = publicBuilt ?? internalBuilt;
    if (!primary) {
      // Unreachable: needsPublicAlb || needsInternalAlb is true for every
      // CloudfrontOriginMode ('public' takes the first branch, the other
      // three take the second). A thrown assertion, not a silent `!`, so a
      // future mode added to the union without updating this file fails
      // loudly at synth instead of producing a stack with no ALB at all.
      throw new Error('AlbStack: neither a public nor an internal ALB was built.');
    }
    this.alb = primary.alb;
    this.targetGroup = primary.targetGroup;
    this.httpListener = primary.httpListener;
    this.httpsListener = primary.httpsListener;

    if (publicBuilt && internalBuilt) {
      this.additionalTargetGroup = internalBuilt.targetGroup;
    }

    applyCommonTags(this, prefix, 'ALB');
  }

  /**
   * Builds one ALB + target group + HTTP(S) listener(s), with every tuned
   * value (health check, idle timeout, the two routing attributes) set
   * exactly once. Called once for the public/private-entry ALB and, when
   * `cloudfrontOrigin` calls for it, a second time for the internal one —
   * so the two can never carry different values for anything this method
   * sets, by construction rather than by a test remembering to compare them
   * (the test in `test/alb-stack-shared-builder.test.ts` asserts it anyway,
   * as a ratchet against a future caller passing divergent values in).
   */
  private buildAlb(opts: BuildAlbOptions): BuiltAlb {
    const idOf = (name: string) => `${opts.idPrefix}${name}`;

    const alb = new elbv2.ApplicationLoadBalancer(this, idOf('Alb'), {
      vpc: opts.vpc,
      internetFacing: !opts.internal,
      securityGroup: opts.securityGroup,
      vpcSubnets: opts.vpcSubnets,
      ...(opts.loadBalancerName ? { loadBalancerName: opts.loadBalancerName } : {}),
      http2Enabled: true,
      // Production defaults to deletion-protected so a stray `cdk destroy`
      // can't wipe the load balancer out from under a live tenant.
      deletionProtection: opts.deletionProtection,
      // AWS's own default is 60s; made explicit so this value cannot
      // silently differ between the public and internal ALB, or drift if
      // AWS ever changes its own default. See `_common.ts`'s doc.
      idleTimeout: cdk.Duration.seconds(ALB_IDLE_TIMEOUT_SECONDS),
    });

    // Every ALB this stack builds gets these two attributes, regardless of
    // entry mode or cloudfrontOrigin mode:
    //
    //   - `drop_invalid_header_fields.enabled=true` makes the ALB silently
    //     DROP (not reject the request for) any header whose NAME does not
    //     match `[-A-Za-z0-9]+` — notably, this excludes any header name
    //     containing an underscore. Before enabling it, this was checked
    //     against every header this gateway and its supported clients
    //     actually use: every header the backend reads
    //     (`backend/mvp/deps.py`, `backend/mvp/observability/context.py`,
    //     `backend/mvp/task_tag.py`, `backend/middleware/correlation.py`,
    //     `backend/core/rate_limit.py`, `backend/mvp/well_known.py`,
    //     `backend/mvp/anthropic.py`) and every header the bundled CLI sends
    //     (`cli/src/mvp/sc_headers.rs`, which validates its own `x-sc-*`
    //     names at construction) is hyphenated, including the whole `x-sc-*`
    //     family (`x-sc-group-id`, `x-sc-workflow-run-id`, `x-sc-session-id`,
    //     `x-sc-task-tag`, `x-sc-model-pin`, `x-sc-fault`). The Codex CLI
    //     integration (`cli/src/mvp/codex_cmd.rs`) does not make HTTP calls
    //     of its own at all — it launches the external `codex` binary and
    //     injects the same `x-sc-*` headers through codex's own
    //     `http_headers` config, so there is no separate header set to
    //     check there either. No underscore-named HTTP header (as opposed
    //     to a JSON body field, which this attribute does not touch) was
    //     found anywhere in that search.
    //   - `desync_mitigation_mode=defensive` is already AWS's own default
    //     for an Application Load Balancer (confirmed against the AWS ELB
    //     documentation; not independently verified on a live ALB in this
    //     account, since this stack makes no AWS calls at synth time).
    //     Setting it explicitly turns an implicit default into something
    //     this template states and a future diff would show moving, rather
    //     than leaving this deployment's posture to whatever AWS's default
    //     happens to be on the day it is created.
    alb.setAttribute('routing.http.drop_invalid_header_fields.enabled', 'true');
    alb.setAttribute('routing.http.desync_mitigation_mode', 'defensive');

    if (opts.enableAccessLogs) {
      const accessLogsBucket = new s3.Bucket(this, idOf('AlbAccessLogsBucket'), {
        bucketName: `${opts.prefix}-alb-access-logs-${this.account}`,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
        autoDeleteObjects: true,
        blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
        encryption: s3.BucketEncryption.S3_MANAGED,
        enforceSSL: true,
        lifecycleRules: [
          {
            id: 'expire-after-90-days',
            enabled: true,
            expiration: cdk.Duration.days(90),
          },
        ],
      });
      alb.logAccessLogs(accessLogsBucket, 'alb-access-logs');
    }

    const targetGroup = new elbv2.ApplicationTargetGroup(this, idOf('BackendTargetGroup'), {
      vpc: opts.vpc,
      port: opts.targetPort,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      ...(opts.idPrefix === ''
        ? { targetGroupName: `${opts.prefix}-backend-tg` }
        : {}),
      // Round robin sends the next request to the next target regardless of how
      // much work each is already carrying, which is wrong for a gateway whose
      // requests last from milliseconds to minutes: a target holding several long
      // streams keeps receiving its share. Least-outstanding-requests routes by
      // in-flight count instead, which is the quantity that saturates a task here.
      loadBalancingAlgorithmType:
        elbv2.TargetGroupLoadBalancingAlgorithmType.LEAST_OUTSTANDING_REQUESTS,
      healthCheck: {
        path: opts.healthCheckPath,
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
        protocol: elbv2.Protocol.HTTP,
      },
      deregistrationDelay: cdk.Duration.seconds(30),
    });

    // P1-2c: `open: false` prevents CDK from auto-adding a
    // `0.0.0.0/0 -> :80` ingress to our ALB security group. Inbound is
    // controlled exclusively by network-stack.ts, which only allows the
    // CloudFront origin-facing managed prefix list (and, for the internal
    // ALB under `entryMode: 'both'`, the internal NLB's security group on
    // :443). Without this flag, CDK would punch a hole we just spent the
    // sweep closing.
    const httpBehavior: HttpBehavior =
      opts.httpBehavior ?? (opts.certificateArn ? 'redirect-to-https' : 'forward');

    let httpListener: elbv2.ApplicationListener | undefined;
    switch (httpBehavior) {
      case 'redirect-to-https':
        httpListener = alb.addListener(idOf('HttpListener'), {
          port: 80,
          protocol: elbv2.ApplicationProtocol.HTTP,
          open: false,
          defaultAction: elbv2.ListenerAction.redirect({
            protocol: 'HTTPS',
            port: '443',
            permanent: true,
          }),
        });
        break;
      case 'forward':
        httpListener = alb.addListener(idOf('HttpListener'), {
          port: 80,
          protocol: elbv2.ApplicationProtocol.HTTP,
          open: false,
          defaultAction: elbv2.ListenerAction.forward([targetGroup]),
        });
        break;
      case 'closed':
        // No :80 listener at all. Only reachable via certificateArn set +
        // httpBehavior explicitly 'closed' (STRATOCLAVE_ENTRY=private with
        // httpRedirectListener: false) — see that prop's doc.
        break;
    }

    let httpsListener: elbv2.ApplicationListener | undefined;
    if (opts.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        idOf('Certificate'),
        opts.certificateArn,
      );
      httpsListener = alb.addListener(idOf('HttpsListener'), {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        open: false,
        certificates: [certificate],
        defaultAction: elbv2.ListenerAction.forward([targetGroup]),
      });
    }

    // XFF handling on the ALB itself (see the prop doc for why `private`
    // passes 'remove'). Unset leaves AWS's own default ('append') in force,
    // so a caller that never passes this — every `cloudfront` deployment
    // today, and the internal VPC-origin ALB — produces zero template diff
    // on this attribute.
    if (opts.xffHeaderProcessingMode) {
      alb.setAttribute(
        'routing.http.xff_header_processing.mode',
        opts.xffHeaderProcessingMode,
      );
    }

    return { alb, targetGroup, httpListener, httpsListener };
  }
}
