# Datadog AWS Integration

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws/main.yaml)
  
## Installation

1. Open the [AWS integration tile](https://app.datadoghq.com/account/settings#integrations/amazon-web-services) within the Datadog platform.
   1. Click "Add an account".
   1. Enter your AWS Account ID, e.g., 123456789012.
   1. Enter IAM Role name `DatadogIntegrationRole` (needs to match the value of `IAMRoleName` in the next step).
   1. Copy the External ID for the next step to use.
1. Log into your admin AWS account/role and deploy the CloudFormation Stack with the button above.
   1. Fill in all the `Required` parameters.
   1. Optinally edit `LogArchives` and `CloudTrails` to configure [Log Archives](https://docs.datadoghq.com/logs/archives/?tab=awss3) and [CloudTrail](https://docs.datadoghq.com/integrations/amazon_cloudtrail/) integration.
   1. On a rare occasion, if you already have a stack deployed in the same AWS account using this template (e.g., monitor the same AWS account in multiple Datadog accounts), You MUST use a different role name for `IAMRoleName` and set `InstallDatadogPolicyMacro` to `false`.
   1. Click **Create stack**.

## AWS Resources

This template creates the following AWS resources required by the Datadog AWS integration:

- An IAM role for Datadog to assume for data collection (e.g., CloudWatch metrics)
- The [Datadog Forwarder Lambda function](https://github.com/DataDog/datadog-serverless-functions/tree/master/aws/logs_monitoring) to ship logs from S3 and CloudWatch, custom metrics and traces from Lambda functions to Datadog
  - The Datadog Forwarder only deploy to the AWS region where the AWS integration CloudFormation stack is launched. If you operate in multiple AWS regions, you can deploy the Forwarder stack (without the rest of the AWS integration stack) directly to other regions as needed.
  - The Datadog Forwarder is installed with default settings as a nested stack, edit the nested stack directly to update the forwarder specific settings.

## Datadog::Integrations::AWS

This CloudFormation stack only manages *AWS* resources required by the Datadog AWS integration. The actual integration configuration within Datadog platform can also be managed in CloudFormation using the custom resource [Datadog::Integrations::AWS](https://github.com/DataDog/datadog-cloudformation-resources/tree/master/datadog-integrations-aws-handler) if you like.

## Terraform

If you prefer managing the AWS resources using Terraform, check out the sample Terraform configuration [datadog_aws_integration.tf](datadog_aws_integration.tf).
