# Datadog AWS Integration

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-streams&templateURL=https://datadog-cloudformation-stream-template.s3.amazonaws.com/aws/streams_main.yaml)

## AWS Resources

This template creates the following AWS resources required for streaming metrics to Datadog. These are created in all regions requested.

- A Cloudwatch Metric Stream to send payloads from Cloudwatch to a Kinesis Firehose
- A Kinesis Firehose to send metrics to Datadog
- A Log Stream and Log Group for logs related to the Kinesis Firehose
- An s3 bucket for storing payloads that fail to get sent to Datadog
- IAM Roles for the above resources
