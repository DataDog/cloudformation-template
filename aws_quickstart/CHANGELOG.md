# 4.4.0 (February 9, 2026)

- Remove all enumerated IAM permissions from the datadog_integration_role stack
- Add Lambda function to retrieve and attach the equivalent IAM permissions at stack creation/update time

# 4.3.0 (December 26, 2025)

### Features

- Add `main_workflow.yaml` CloudFormation template with workflow status tracking stack progression in Datadog UI launches

# 4.1.6 (October 31, 2025)

[Agentless] Send scanner instance policy ARN to backend to reduce first scan latency

# 4.1.5 (October 27, 2025)

Add permissions to support AWS Batch integration:

- `batch:DescribeJobs`
- `batch:ListJobs`

# 4.1.4 (October 22, 2025)

Add permissions to support resource collection for the following services:

- AWS AppConfig
- Amazon AppFlow
- Amazon CloudWatch Application Signals
- AWS Billing Conductor
- AWS Clean Rooms
- AWS Fault Ingestion Service
- Amazon Kendra
- AWS License Manager
- AWS Elemental Mediaconvert
- AWS Elemental Mediatailor
- AWS Health Omics
- AWS Identity and Access Management Roles Anywhere
- Amazon Route 53 Profiles
- AWS Serverless Application Repository
- AWS Well-Architected Tool
- Amazon Q in Connect (wisdom)

## 4.1.3 (September 16, 2025)

Add support for more log sources for Log autosubscription. Preparing for the next release covering these services:

- AppSync (CW)
- Batch (CW)
- CodeBuild (CW & S3)
- Database Migration Service (CW)
- DocDB (CW)
- ECS (CW)
- Route53 Resolver (CW & S3)
- Verified Access (CW & S3)
- VPC Flow logs (CW & S3)
- VPN (CW)

AWS updated the Security Audit policy. As a result, we're updating our sets of permissions for resource collection to match the ones already covered by that policy.

## 4.1.2 (September 9, 2025)

Add permission `redshift-serverless:GetSnapshot` to support extensive security audits for that service

## 4.1.1 (September 1, 2025)

Add permissions to support resource collection for the following services:

- AWS Audit Manager
- Amazon CodeGuru
- AWS Device Farm
- Amazon Fraud Detector
- Amazom GameLift
- AWS IoT Greengrass
- AWS IoT FleetWise
- AWS Lake Formation
- AWS Migration Hub Refactor Spaces
- Amazon Textract
- Amazon Workspaces Secure Browser

## 4.1.0 (September 1, 2025)

Agentless scanning fixes and improvements:

- Fix for unsupported attribute type `PolicyArn` in `Fn::GetAtt`
- Better handling of unexpected status codes in (de)activation call

## 4.0.3 (August 20, 2025)

Add permissions to support log autosubscription collection for the following services:

- AWS Network Firewall
- Amazon Elastic Kubernetes Services
- Amazon Redshift Serverless
- Amazon Parallel Computing Service (log deliveries change)

## 4.0.2 (August 19, 2025)

Add permissions to support resource collection for the following services:

- Amazon Location (`geo`)
- AWS Elemental Mediapackage, Mediapackage V2 and Mediapackage VOD
- AWS Snowball
- AWS SSM incident manager
- Amazon Transcribe
- Amazon Verified Permissions

## 4.0.1 (August 8, 2025)

[Agentless] Send ASG ARN and Launch Template ID to backend

## 4.0.0 (August 6, 2025)

- Update inline policy to include core permissions only.
- Add attached policies for resource collection permissions.

## 3.1.2 (August 4, 2025)

[Agentless] Allow specifying delegate role name to quickstart main_extended

## 3.1.1 (July 24, 2025)

[Agentless] Send ARNs of Agentless IAM resources to backend

## 3.1.0 (July 15, 2025)

[Agentless] Send role ARNs in Agentless API call

## 3.0.0 (July 9, 2025)

Align cloudformation template permissions with generated docs

## 2.2.7 (July 1, 2025)

Add GetAccountInformation rule to integration policy

## 2.2.6 (July 1, 2025)

[Agentless] Fix race-condition when creating route tables

## 2.2.4 (June 25, 2025)

[Agentless] CF template extending delegate role to allow snapshot copies

## 2.2.3 (June 13, 2025)

Update templates to support new AP2 datacenter

## 2.2.2 (June 12, 2025)

[Agentless] Adapt CopySnapshot policy to latest IAM changes

## 2.2.1 (June 6, 2025)

[Agentless] Report template version when activating Agentless

## 2.2.0 (June 3, 2025)

[Agentless] Add cross-account scanning to main_extended for Agentless

## 2.1.13 (May 28, 2025)

[Agentless] Add Agentless API call to delegate role template

## 2.1.12 (May 20, 2025)

[Agentless] Add check to validate AWS Account ID in Agentless template.

## 2.1.11 (May 5, 2025)

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
