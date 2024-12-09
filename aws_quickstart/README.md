# Datadog AWS Integration

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/quickcreate?param_APIKey=&param_APPKey=&param_CloudSecurityPostureManagement=false&param_DatadogSite=datadoghq.com&param_InstallLambdaLogForwarder=true&stackName=DatadogIntegration&templateURL=https%3A%2F%2Fdatadog-cloudformation-template-quickstart.s3.amazonaws.com%2Faws%2Fv2.0.5%2Fmain_v2.yaml)
  
## Installation

1. Open the [AWS integration tile](https://app.datadoghq.com/account/settings#integrations/amazon-web-services) within the Datadog platform.
   1. Click "Add AWS Account(s)".
   1. Fill out the various fields under "Automatically using CloudFormation".
   1. Click "Launch CloudFormation Template" in the bottom right.
1. Log into your admin AWS account/role and deploy the CloudFormation Stack that you're linked to. All required parameters will be filled in.
   1. Optinally edit "Advanced" fields to set the IAM RoleName used, and disable default collection settings.
   1. On a rare occasion, if you already have a stack deployed in the same AWS account using this template (e.g., monitor the same AWS account in multiple Datadog accounts), You MUST use a different role name for `IAMRoleName`.
   1. Check the 2 checkboxes under "Capabilities" to give our stack the necessary permissions.
   1. Click **Create stack**.

## AWS Resources

This template creates the following AWS resources required by the Datadog AWS integration:

- An IAM role for Datadog to assume for data collection (e.g., CloudWatch metrics)
- The [Datadog Forwarder Lambda function](https://github.com/DataDog/datadog-serverless-functions/tree/master/aws/logs_monitoring) to ship logs from S3 and CloudWatch, custom metrics and traces from Lambda functions to Datadog
  - The Datadog Forwarder only deploys to the AWS region where the AWS integration CloudFormation stack is launched. If you operate in multiple AWS regions, you can deploy the Forwarder stack (without the rest of the AWS integration stack) directly to other regions as needed.
  - The Datadog Forwarder is installed with default settings as a nested stack, edit the nested stack directly to update the forwarder specific settings.

## Updating your CloudFormation Stack

As of v2.0.0 of the aws_quickstart template updates to the stack parameters are supported. Updates should generally be made to the root CloudFormation Stack (entitled DatadogIntegration by default). We do not support updating the API Key or APP Key fields to point to a different Datadog Organization - if these are updated they must point to the original Organization. You can also update the version of the template used by selecting "Replace existing template" while updating your CloudFormation Stack. You must select a version number with the same major version as your current template.

**Note:** Updating from v1.X.X -> v2.X.X is not supported. If you are on v1.X.X you can delete your original stack and recreate a new stack with the v2.X.X template, however this is dangerous as you will be deleting your Datadog integration for this account, and you will experience a temporary service disruption in Datadog until the v2.X.X stack is successfully created.

## Datadog::Integrations::AWS

This CloudFormation stack only manages *AWS* resources required by the Datadog AWS integration. The actual integration configuration within Datadog platform can also be managed in CloudFormation using the custom resource [Datadog::Integrations::AWS](https://github.com/DataDog/datadog-cloudformation-resources/tree/master/datadog-integrations-aws-handler) if you like.

## Terraform

If you prefer managing the AWS resources using Terraform, check out the sample Terraform configuration [datadog_aws_integration.tf](datadog_aws_integration.tf).
