import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import { applyCommonTags, putStringParameter } from './_common';

export interface CognitoStackProps extends cdk.StackProps {
  prefix: string;
  /**
   * Cognito Hosted UI のドメインプレフィックス（グローバル一意）
   * 未指定なら `${prefix}-auth` + ランダムサフィックスを自動生成
   */
  domainPrefix?: string;
  /** 本番 CloudFront ドメイン（callback URL 登録用） */
  cloudFrontDomainName?: string;
  additionalCallbackUrls?: string[];
  additionalLogoutUrls?: string[];
}

/**
 * MVP Cognito Stack
 *
 * - User/Pass 認証を有効化（`USER_PASSWORD_AUTH` + `ADMIN_USER_PASSWORD_AUTH`）
 * - Admin が `AdminCreateUser` でユーザー作成（temp password 自動生成、メール送信は SUPPRESS）
 * - 初回ログイン時 `NEW_PASSWORD_REQUIRED` で CLI が対話的にパスワード変更
 * - OAuth 2.0 + PKCE は Frontend (Cognito Hosted UI) 用にも保持
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
        minLength: 8,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.days(7),
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
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
        userPassword: true, // CLI で直接 User/Pass 認証を行うため
        adminUserPassword: true, // Backend が AdminInitiateAuth するため
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
      refreshTokenValidity: cdk.Duration.days(30),
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
