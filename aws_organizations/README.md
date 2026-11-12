# Datadog AWS Integration with Organizations

This guide provides an overview of the process for integrating multiple accounts within your AWS Organization or Organizational Unit with Datadog.

The Cloudformation StackSet template provided by Datadog automates the creation of the required IAM role and associated policy in every AWS account under an Organization or Organizational Unit (OU), eliminating the need for manual setup and configuration.

The Datadog Cloudformation StackSet performs the following steps:

- Deploys the Datadog AWS integration CloudFormation StackSet template in the root account of an AWS Organization or Organization Unit.
- Automatically creates the necessary IAM role and policies in the target accounts.
- Automatically initiates ingestion of AWS CloudWatch metrics and events from the AWS resources in the accounts.
- Optionally configures Datadog Cloud Security Management to monitor resource misconfigurations in your AWS accounts.
- Optionally installs Agentless SaaS permissions and initializes Agentless scanning in each target account.

## Prerequisites

Before getting started, ensure you have the following prerequisites:
- Access to the management account: Your AWS user needs to be able to access the management account.
- An account administrator has enabled Trusted access with AWS Organizations: Refer to AWS Docs on how to enable trusted access between StackSets and Organizations to create & deploy stacks using service-managed permissions.
  
## Installation

1. Go to the AWS integration configuration page in Datadog and click "Add AWS Account".
2. Select the integration’s settings under the “Add Multiple AWS Accounts” option. You will need these in Step 3.
   1. Use the template specified in this step in the newly created Cloudformation Stackset.
   2. Select your Datadog API key.
   3. Select your Datadog APP key.
3. Click [Launch CloudFormation Template](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacksets/create). This opens the AWS Console and loads the CloudFormation StackSet.
4. Keep the default choice of "Service-managed permissions".
5. Under Specify template, paste the template URL from Step 2 and click Next.
6. Name your StackSet and fill in the API and APP keys you selected in Step 2.
   1. Optionally, enable Cloud Security Posture Management (CSPM) to scan your cloud environment, hosts, and containers for misconfigurations and security risks.
   2. Optionally, set `AgentlessScanning` to `true`. Set `AgentlessSensitiveDataScanning` to `true` to also initialize sensitive data scanning. Both options default to `false`; see the lifecycle details below.
7. In Step 3 of the StackSet template, choose to either deploy the Datadog integration across an Organization or a specific Organizational Unit.
8. Keep Automatic deployment enabled in order to automatically deploy the Datadog AWS Integration in new accounts that are added to the Organization or OU.
9. Select which regions in which you’d like to deploy the integration. Note that you can modify regions to monitor from the Datadog AWS configuration page after deploying the stack.
10. Move to the Review page and Click Submit. This launches the creation process for the Datadog StackSet. This could take a while depending on how many accounts need to be integrated. Ensure that the StackSet successfully creates all resources before proceeding.
11. After the stack is created, go back to the AWS integration tile in Datadog and click Ready!

## Datadog::Integrations::AWS

This CloudFormation StackSet only manages *AWS* resources required by the Datadog AWS integration. The actual integration configuration within Datadog platform can also be managed in CloudFormation using the custom resource [Datadog::Integrations::AWS](https://github.com/DataDog/datadog-cloudformation-resources/tree/master/datadog-integrations-aws-handler) if you like.

## Agentless SaaS scanning

Agentless SaaS must be available for your organization. It requires `DisableResourceCollection=false`, a commercial AWS partition and Datadog site, and an application key permitted to manage the integration and write Agentless scan options.

| Parameter | Default | Behavior |
| --- | --- | --- |
| `AgentlessScanning` | `false` | Installs SaaS scanning permissions and initializes vulnerability scanning for hosts, containers, and Lambda functions, plus host compliance scanning. |
| `AgentlessSensitiveDataScanning` | `false` | Selects whether sensitive data scanning is enabled during initialization. Ignored when `AgentlessScanning=false`. |

Initialization **overwrites existing scan settings**, including those used by self-hosted scanners. It runs on first enablement and resource recreation, including disabling and re-enabling Agentless. Automatically enrolled accounts use the current StackSet parameters.

Manage subsequent settings in Datadog: stack updates, including sensitive-data parameter changes, do not reapply them. The full SaaS IAM policy is installed with either sensitive-data choice.

Activation is best effort. API failures are logged in the API Lambda's CloudWatch logs and do not fail stack creation. Verify activation in Datadog; IAM discovery is asynchronous. To retry, configure scanning in Datadog or disable and re-enable Agentless.

Disabling Agentless removes the stack-owned policy but retains scan settings. Activation deletion and rollback also retain settings. Policy removal takes time to be discovered; other policies or self-hosted scanners may still permit scanning.

Upgrade existing StackSet-managed accounts to enable this feature. Do not deploy this integration StackSet over accounts whose integration is managed separately.
