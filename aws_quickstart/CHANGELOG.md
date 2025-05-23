## 2.1.11 (May 21, 2025)

Adding support for Govcloud AWS Account ID in AssumeRole policy.

## 2.1.10 (March 13, 2025)

### Features

Adding permissions to support resource collection for the following services:

- S3 tables

## 2.1.9 (March 13, 2025)

Aligning template versions

## 2.1.8 (March 13, 2025)

### Features

Adding permissions to support resource collection for the following services:

- Amplify
- Appstream
- Batch
- Deadline Cloud
- Identity Store
- Image builder (EC2)
- Pinpoint (mobiletargeting)
- SMS Voice
- Social messaging

## 2.1.7 (February 26, 2025)

### Features

Adding permissions to support resource collection for the following services:

- EMR Containers
- Grafana
- IoT Greengrass v2
- Macie
- Opensearch Ingestion (osis)
- Opensearch serverless
- QLDB Ledgers
- Proton
- SESv2
- SES Mail manager
- Workmail

## 2.1.6 (February 26, 2025)

### Features

Adding permissions to support resource collection for the following services:

- SES
- Codepipeline
- Codeartifact
- Managed Blockchain
- Redshift Serverless

## 2.1.4 (February 19, 2025)

### Features

- Adding MemoryDB Describe permissions to support resource collection coverage

## 2.1.3 (February 11, 2025)

### Features

- Adding Bedrock Get and List permissions support Bedrock resource collection coverage

## 2.1.0 (January 21, 2025 )

### Features

- Add `ec2:GetAllowedImagesSettings` to integration role policy

## 2.0.9 (December 13, 2024)

### Features

- Fixing permissions for Amazon Keyspaces (for Apache Cassandra)

## 2.0.7 (December 13, 2024)

### Features

- Add additional permissions empowering AWS resource collection and align policy content between templates

## 2.0.6 (December 4, 2024)

### Features

- Add `s3express` permission for Resource Crawler

## 2.0.2 (November 13, 2024)

### Features

- Add GetResourcePolicy permission for secretsmanager

## 1.2.5 (November 5, 2024)

### FEATURES

- Adds additional permission

## 1.2.5 (November 5, 2024)

### FEATURES

- Added DisableResourceCollection field which defaults to False. When False, SecurityAudit policy will be attached to the created Datadog IAM Role, and ExtendedResourceCollection will be enabled in Datadog.
- Added additional permissions to the default set of permissions attached to the created Datadog IAM Role.
