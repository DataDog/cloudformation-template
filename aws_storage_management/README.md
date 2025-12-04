# Datadog AWS Integration Storage Management Permissions

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-storage-management-permissions&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws_storage_management/v1.0.0/main.yaml)

## Overview

This CloudFormation template attaches S3 permissions to your Datadog integration role for Storage Management. It creates a managed IAM policy that allows Datadog to collect S3 bucket metadata, configure inventory, and read inventory reports.

## Prerequisites

- Existing [Datadog AWS Integration](https://docs.datadoghq.com/integrations/amazon_web_services/)
- S3 destination bucket(s) for inventory reports

## Parameters

- **DatadogIntegrationRole** - Name of your Datadog integration IAM role
- **AccountId** - Your AWS account ID
- **DestinationBucketResources** - Comma-separated S3 bucket ARNs (must include both bucket ARN and bucket/* ARN for each bucket)
  - Example: `arn:aws:s3:::my-bucket,arn:aws:s3:::my-bucket/*`

## Installation

```bash
aws cloudformation create-stack \
  --stack-name datadog-storage-management-permissions \
  --template-body file://main.yaml \
  --parameters \
    ParameterKey=DatadogIntegrationRole,ParameterValue=DatadogIntegrationRole \
    ParameterKey=AccountId,ParameterValue=123456789012 \
    ParameterKey=DestinationBucketResources,ParameterValue="arn:aws:s3:::my-bucket,arn:aws:s3:::my-bucket/*" \
  --capabilities CAPABILITY_IAM
```

## Permissions Granted

- S3 bucket metadata and configuration (all buckets)
- S3 inventory configuration (all buckets)
- S3 bucket policy configuration (allows Datadog to set up destination bucket policies automatically)
- Read access to inventory reports (scoped to destination buckets only)

See the [Storage Management documentation](https://docs.datadoghq.com/infrastructure/storage_management/amazon_s3) for complete setup and permission details.
