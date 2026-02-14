# Datadog AWS Integration Permissions

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws_attach_integration_permissions/v1.0.0/main.yaml)

## Overview

This CloudFormation template manages IAM permissions for the Datadog AWS integration. It fetches [all required permissions for the integration](https://docs.datadoghq.com/integrations/amazon_web_services/#aws-iam-permissions) from the Datadog API and creates customer managed IAM policies which are attached to your Datadog integration role.

## Installation

1. Ensure you have already set up the [Datadog AWS Integration](https://docs.datadoghq.com/integrations/amazon_web_services/).
2. Deploy this CloudFormation stack in the same AWS account as your Datadog integration.
3. Provide the following parameters:
   - `DatadogIntegrationRole`: The name of your existing Datadog integration IAM role
   - `AccountId`: Your AWS account ID

## AWS Resources

This template creates the following AWS resources:

- Customer managed IAM policies:
  - Policies are named in the format `{RoleName}-ManagedPolicy-{n}` (e.g., `DatadogIntegrationRole-ManagedPolicy-1`).
  - Each policy contains up to 150 permissions to stay within IAM policy character limits.
  - The SecurityAudit AWS managed policy is also attached to your Datadog integration role.
