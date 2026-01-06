# Datadog AWS Cloud Cost Setup (CUR 2.0)

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](ADDLINK)

## Overview

This template sets up AWS Cloud Cost Management for Datadog using **CUR 2.0 (Data Exports)**. It creates the necessary AWS resources and configures the Datadog integration automatically.

## AWS Resources

This template creates the following:

- **S3 Bucket** (optional) - For storing cost and usage data
  - Bucket policy allowing `bcm-data-exports.amazonaws.com` to write reports
- **BCM Data Export** (optional) - CUR 2.0 export with hourly granularity in Parquet format
- **IAM Policy** - Grants Datadog read access to the S3 bucket and Cost Explorer APIs
  - Attached to your existing Datadog integration role
- **Lambda Function** - Calls the Datadog API to configure CCM with the data export details

## Parameters

| Parameter | Description | Required |
|-----------|-------------|----------|
| `DatadogApiKey` | Datadog API key | Yes |
| `DatadogAppKey` | Datadog APP key | Yes |
| `DatadogSite` | Datadog site (e.g., datadoghq.com) | No (default: datadoghq.com) |
| `CloudCostBucketName` | S3 bucket name for cost reports | Yes |
| `CloudCostBucketRegion` | AWS region of the S3 bucket | Yes |
| `CloudCostReportPrefix` | Prefix for report files in S3 | Yes |
| `CloudCostReportName` | Name of the data export | Yes |
| `DatadogIntegrationRole` | Name of your Datadog integration IAM role | Yes |
| `CreateCloudCostReport` | Create a new data export (true/false) | No (default: true) |
| `CreateCloudCostBucket` | Create a new S3 bucket (true/false) | No (default: true) |

## Publishing the Template

Use the release script to upload the template to an S3 bucket:

```sh
./release.sh <bucket_name>
```

Use `--private` to prevent granting public access (useful for testing):

```sh
./release.sh <bucket_name> --private
```

The template will be available at `s3://<bucket_name>/aws_cloud_cost_cur2/<version>/main.yaml`.
