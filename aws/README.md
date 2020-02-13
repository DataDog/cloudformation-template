# Datadog AWS Integration

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws/main.yaml)

Create the following AWS resources required by the Datadog AWS integration
- An IAM role (with either Full or Core permissions) for Datadog to assume for data collection (e.g., CloudWatch metrics).
- If a list of S3 paths for log archives are given, automatically adds the required permissions to the integration IAM role.
- The [Datadog Forwarder Lambda function](https://github.com/DataDog/datadog-serverless-functions/tree/master/aws/logs_monitoring).
  - The Datadog Forwarder only deploy to the AWS region where the AWS integration CloudFormation stack is launched. If you operate in multiple AWS regions, you can deploy the Forwarder stack (without the rest of the AWS integration stack) directly to other regions as needed.
  - The Datadog Forwarder is installed with default settings, edit the Forwarder stack (or nested stack) directly to update the forwarder specific settings.

Note, this CloudFormation stack only manages *AWS* resources required by the integration. The actual integration configuration within Datadog platform can also be managed in CloudFormation using the custom resource [Datadog::Integrations::AWS](https://github.com/DataDog/datadog-cloudformation-resources/tree/master/datadog-integrations-aws-handler).
