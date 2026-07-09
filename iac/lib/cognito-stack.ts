import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface CognitoStackProps extends cdk.StackProps {
  prefix: string;
  /**
   * Cognito Hosted UI domain prefix (globally unique).
   * If not specified, auto-generated as `${prefix}-auth` + a random suffix.
   */
  domainPrefix?: string;
  /** Production CloudFront domain (used to register callback URLs) */
  cloudFrontDomainName?: string;
  additionalCallbackUrls?: string[];
  additionalLogoutUrls?: string[];
  /**
   * Environment name (development, staging, production).
   *
   * Drives the User Pool removal policy (A-20-cognito) and the refresh-
   * token TTL (A-09-cognito). When omitted, defaults to `development`
   * so a fresh stack stays disposable. Production deployments MUST
   * pass `production` so the pool retains on stack delete and refresh
   * tokens cap at 7 days.
   */
  environment?: string;
}

/**
 * MVP Cognito Stack
 *
 * - User/Password auth enabled (`USER_PASSWORD_AUTH` + `ADMIN_USER_PASSWORD_AUTH`)
 * - Admin creates users via `AdminCreateUser` (temp password auto-generated, email delivery SUPPRESSED)
 * - On first login the CLI handles `NEW_PASSWORD_REQUIRED` interactively
 * - OAuth 2.0 + PKCE retained for Frontend (Cognito Hosted UI)
 */
export class CognitoStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly userPoolDomain: cognito.UserPoolDomain;

  public readonly userPoolId: string;
  public readonly clientId: string;
  public readonly cognitoDomainUrl: string;
  public readonly oidcIssuerUrl: string;
  public readonly domainPrefix: string;

  constructor(scope: Construct, id: string, props: CognitoStackProps) {
    super(scope, id, props);

    const { prefix } = props;
    const env = props.environment || 'development';
    const isProd = env === 'production';
    this.domainPrefix = props.domainPrefix || `${prefix}-auth-${cdk.Aws.ACCOUNT_ID}`;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `${prefix}-user-pool`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      customAttributes: {
        org_id: new cognito.StringAttribute({
          mutable: true,
          minLen: 1,
          maxLen: 256,
        }),
      },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.days(7),
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      // A-20-cognito: production must RETAIN the User Pool on stack
      // delete; losing it orphans every issued credential and forces a
      // full re-onboarding. Dev still uses DESTROY so disposable
      // stacks tear down cleanly.
      removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    this.userPoolDomain = this.userPool.addDomain('Domain', {
      cognitoDomain: { domainPrefix: this.domainPrefix },
    });

    const callbackUrls: string[] = [
      'http://127.0.0.1:18080/callback',
      'http://localhost:3003/callback',
    ];
    if (props.cloudFrontDomainName) {
      callbackUrls.push(`https://${props.cloudFrontDomainName}/callback`);
    }
    if (props.additionalCallbackUrls) {
      callbackUrls.push(...props.additionalCallbackUrls);
    }

    const logoutUrls: string[] = [
      'http://127.0.0.1:18080',
      'http://localhost:3003',
    ];
    if (props.cloudFrontDomainName) {
      logoutUrls.push(`https://${props.cloudFrontDomainName}`);
    }
    if (props.additionalLogoutUrls) {
      logoutUrls.push(...props.additionalLogoutUrls);
    }

    this.userPoolClient = this.userPool.addClient('Client', {
      userPoolClientName: `${prefix}-client`,
      generateSecret: false,
      authFlows: {
        userSrp: true,
        userPassword: true, // required for direct User/Pass auth from the CLI
        adminUserPassword: true, // required for the backend to call AdminInitiateAuth
      },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
        ],
        callbackUrls,
        logoutUrls,
      },
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
      ],
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      // A-09-cognito: cap refresh-token TTL at 7 days in production so
      // a stolen refresh token is invalidated within a week even if
      // the global sign-out path fails. Dev keeps the legacy 30-day
      // window for ergonomic development workflows.
      refreshTokenValidity: isProd ? cdk.Duration.days(7) : cdk.Duration.days(30),
      preventUserExistenceErrors: true,
    });

    this.userPoolId = this.userPool.userPoolId;
    this.clientId = this.userPoolClient.userPoolClientId;
    this.cognitoDomainUrl = `https://${this.domainPrefix}.auth.${this.region}.amazoncognito.com`;
    this.oidcIssuerUrl = `https://cognito-idp.${this.region}.amazonaws.com/${this.userPool.userPoolId}`;

    putStringParameter(this, 'CognitoUserPoolIdParam', {
      prefix,
      relativePath: 'cognito/user-pool-id',
      value: this.userPoolId,
      description: 'Cognito User Pool ID',
    });
    putStringParameter(this, 'CognitoClientIdParam', {
      prefix,
      relativePath: 'cognito/client-id',
      value: this.clientId,
      description: 'Cognito App Client ID',
    });
    putStringParameter(this, 'CognitoDomainParam', {
      prefix,
      relativePath: 'cognito/domain',
      value: this.cognitoDomainUrl,
      description: 'Cognito Hosted UI domain',
    });
    putStringParameter(this, 'CognitoIssuerParam', {
      prefix,
      relativePath: 'cognito/oidc-issuer',
      value: this.oidcIssuerUrl,
      description: 'OIDC issuer URL',
    });
    putStringParameter(this, 'CognitoRegionParam', {
      prefix,
      relativePath: 'cognito/region',
      value: this.region,
      description: 'Cognito region',
    });

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: this.clientId });
    new cdk.CfnOutput(this, 'CognitoDomain', { value: this.cognitoDomainUrl });
    new cdk.CfnOutput(this, 'OidcIssuerUrl', { value: this.oidcIssuerUrl });
    new cdk.CfnOutput(this, 'CallbackUrls', { value: callbackUrls.join(', ') });

    applyCommonTags(this, prefix, 'Cognito');
  }
}
