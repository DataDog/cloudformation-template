# Datadog Bits Infrastructure Operations Permissions

## Overview

This CloudFormation template attaches the permissions required by Datadog Bits Infrastructure Operations (BIO) to an existing Datadog AWS integration role. A single template supports the ECS and Serverless products while keeping each deployed stack limited to one product's permission set.

## Installation

Deploy the template in the same AWS account as the Datadog AWS integration and provide:

- `Product`: `ecs` or `serverless`
- `DatadogIntegrationRole`: the existing Datadog integration IAM role name
- `AccountId`: the integrated AWS account ID

Deploy separate stacks for ECS and Serverless when both products are enabled.

## AWS resources

The stack creates one customer-managed IAM policy and attaches it to the existing Datadog integration role. Deleting the stack removes that policy without modifying other policies attached to the role.
