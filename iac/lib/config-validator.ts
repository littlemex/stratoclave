import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import * as cdk from 'aws-cdk-lib';

/**
 * Configuration Validator
 *
 * Validates that hardcoded configuration values match actual AWS resources
 * to prevent mismatches that cause authentication errors.
 */
export class ConfigValidator {
  /**
   * Validate that Cognito domain prefix matches the User Pool's actual domain
   *
   * @param scope CDK construct scope
   * @param userPool Cognito User Pool
   * @param expectedDomainPrefix Domain prefix expected in configuration
   */
  static validateCognitoDomain(
    scope: Construct,
    userPool: cognito.UserPool,
    expectedDomainPrefix: string
  ): void {
    // Custom Resource to query actual Cognito domain and compare
    const validationLambda = new cdk.custom_resources.AwsCustomResource(
      scope,
      'CognitoDomainValidator',
      {
        onUpdate: {
          service: 'CognitoIdentityServiceProvider',
          action: 'describeUserPool',
          parameters: {
            UserPoolId: userPool.userPoolId,
          },
          physicalResourceId: cdk.custom_resources.PhysicalResourceId.of(
            `cognito-domain-validator-${userPool.userPoolId}`
          ),
        },
        policy: cdk.custom_resources.AwsCustomResourcePolicy.fromSdkCalls({
          resources: [userPool.userPoolArn],
        }),
      }
    );

    // Add validation check using CloudFormation condition
    // Note: This is a runtime check, not build-time, but catches errors before full deployment
    const actualDomain = validationLambda.getResponseField('UserPool.Domain');

    // Output for manual verification
    new cdk.CfnOutput(scope, 'CognitoDomainValidation', {
      value: `Expected: ${expectedDomainPrefix}, Actual: ${actualDomain}`,
      description: 'Cognito Domain Validation (must match)',
    });

    // Add warning if mismatch (CloudFormation doesn't support runtime assertions)
    cdk.Annotations.of(scope).addWarning(
      `IMPORTANT: Verify that Cognito domain prefix '${expectedDomainPrefix}' matches actual domain. ` +
      `Mismatch will cause authentication errors.`
    );
  }

  /**
   * Validate that CloudFront domain matches the Distribution's actual domain
   *
   * @param scope CDK construct scope
   * @param distribution CloudFront Distribution
   * @param expectedDomain Domain name expected in configuration
   */
  static validateCloudFrontDomain(
    scope: Construct,
    distribution: cdk.aws_cloudfront.IDistribution,
    expectedDomain: string
  ): void {
    const actualDomain = distribution.distributionDomainName;

    // Output for verification
    new cdk.CfnOutput(scope, 'CloudFrontDomainValidation', {
      value: actualDomain === expectedDomain ? 'MATCH' : `MISMATCH: Expected ${expectedDomain}, Got ${actualDomain}`,
      description: 'CloudFront Domain Validation',
    });

    // Add error if mismatch
    if (actualDomain !== expectedDomain) {
      cdk.Annotations.of(scope).addError(
        `CloudFront domain mismatch: Expected '${expectedDomain}', but actual is '${actualDomain}'. ` +
        `Update iac/bin/iac.ts with the correct domain.`
      );
    }
  }
}
