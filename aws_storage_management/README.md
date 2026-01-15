# Datadog AWS Integration Storage Management Permissions

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-storage-management-permissions&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws_storage_management/v1.0.0/main.yaml)

## Overview

This CloudFormation template attaches S3 permissions to your Datadog integration role for Storage Management. It creates a managed IAM policy that allows Datadog to collect S3 bucket metadata, configure inventory, and read inventory reports.

This template supports both same-account and cross-account inventory configurations.

## Prerequisites

- Existing [Datadog AWS Integration](https://docs.datadoghq.com/integrations/amazon_web_services/) in each AWS account
- S3 destination bucket(s) for inventory reports

## Parameters

- **DatadogIntegrationRole** - Name of your Datadog integration IAM role
- **AccountId** - Your AWS account ID
- **DestinationBucketResources** - Comma-separated S3 bucket ARNs (must include both bucket ARN and bucket/* ARN for each bucket)
  - Example: `arn:aws:s3:::my-bucket,arn:aws:s3:::my-bucket/*`

## Deployment Scenarios

### Scenario 1: Same Account (source and destination buckets in same account)

Deploy the CloudFormation template **once** in the account containing both source and destination buckets:

```bash
aws cloudformation create-stack \
  --stack-name datadog-storage-management-permissions \
  --template-body file://main.yaml \
  --parameters \
    ParameterKey=DatadogIntegrationRole,ParameterValue=DatadogIntegrationRole \
    ParameterKey=AccountId,ParameterValue=123456789012 \
    ParameterKey=DestinationBucketResources,ParameterValue="arn:aws:s3:::my-destination-bucket,arn:aws:s3:::my-destination-bucket/*" \
  --capabilities CAPABILITY_IAM
```

### Scenario 2: Cross-Account (source and destination buckets in different accounts)

Deploy the CloudFormation template in **both accounts**:

**Step 1: Deploy in source account (where buckets to monitor exist)**

```bash
aws cloudformation create-stack \
  --stack-name datadog-storage-management-permissions \
  --template-body file://main.yaml \
  --parameters \
    ParameterKey=DatadogIntegrationRole,ParameterValue=DatadogIntegrationRole \
    ParameterKey=AccountId,ParameterValue=111111111111 \
    ParameterKey=DestinationBucketResources,ParameterValue="arn:aws:s3:::destination-bucket-in-another-account,arn:aws:s3:::destination-bucket-in-another-account/*" \
  --capabilities CAPABILITY_IAM
```

**Step 2: Deploy in destination account (where inventory reports are stored)**

```bash
aws cloudformation create-stack \
  --stack-name datadog-storage-management-permissions \
  --template-body file://main.yaml \
  --parameters \
    ParameterKey=DatadogIntegrationRole,ParameterValue=DatadogIntegrationRole \
    ParameterKey=AccountId,ParameterValue=222222222222 \
    ParameterKey=DestinationBucketResources,ParameterValue="arn:aws:s3:::destination-bucket,arn:aws:s3:::destination-bucket/*" \
  --capabilities CAPABILITY_IAM
```

## Permissions Granted

- **S3 bucket metadata and configuration** (all buckets) - Discover and monitor buckets
- **S3 inventory configuration** (all buckets) - Enable inventory on source buckets
- **S3 bucket policy configuration** (scoped to destination buckets) - Allows Datadog to automatically configure destination bucket policies
- **Read access to inventory reports** (scoped to destination buckets) - Read inventory data

**Note:** For cross-account setups, `s3:PutBucketPolicy` only works in the destination account. Deploy the template in both accounts to grant the appropriate permissions in each.

## Updating Destination Buckets

To add or change destination buckets after deployment, update the stack with new bucket ARNs:

```bash
aws cloudformation update-stack \
  --stack-name datadog-storage-management-permissions \
  --use-previous-template \
  --parameters \
    ParameterKey=DatadogIntegrationRole,UsePreviousValue=true \
    ParameterKey=AccountId,UsePreviousValue=true \
    ParameterKey=DestinationBucketResources,ParameterValue="arn:aws:s3:::bucket1,arn:aws:s3:::bucket1/*,arn:aws:s3:::bucket2,arn:aws:s3:::bucket2/*" \
  --capabilities CAPABILITY_IAM
```

---

See the [Storage Management documentation](https://docs.datadoghq.com/infrastructure/storage_management/amazon_s3) for complete setup and permission details.
