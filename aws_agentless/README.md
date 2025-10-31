# Datadog Agentless Scanning CloudFormation Templates

This directory contains CloudFormation templates for deploying Datadog Agentless Scanning in AWS accounts.

Datadog Agentless Scanning enables vulnerability scanning of your AWS resources (hosts, containers, Lambda functions, and sensitive data in S3 buckets) without requiring the installation of agents.

## Templates

- **`datadog_agentless_scanning.yaml`**: Main template for deploying the complete Agentless Scanner infrastructure, including EC2 instances, Auto Scaling Groups, IAM roles, and optional VPC resources.
- **`datadog_agentless_delegate_role.yaml`**: Template for creating a delegate role that allows the Agentless Scanner from one account to scan resources in another account (cross-account scanning).
- **`datadog_agentless_delegate_role_snapshot.yaml`**: Template that extends an existing delegate role with permissions to copy snapshots across regions.
- **`datadog_agentless_api_call.py`**: Python Lambda function that registers the Agentless Scanning features with the Datadog API.
- **`datadog_agentless_api_call_test.py`**: Unit tests for the API call Lambda function.

## Prerequisite## Release Process

**Important:** These templates must be released to the `datadog-cloudformation-template` S3 bucket before they can be used by customers.

### Releasing a New Version

1. Update the version in `version.txt`
2. Run the release script:
   ```bash
   ./release.sh <S3_BUCKET_NAME>
   ```
   Example:
   ```bash
   ./release.sh datadog-cloudformation-template
   ```
3. Update the pinned version in `aws_quickstart/main_extended.yaml` 