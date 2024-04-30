# Datadog AWS Config Change Stream

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-config-stream&templateURL=https://config-stream-template.s3.amazonaws.com/aws/main_config_stream.yaml)

## AWS Resources

This template creates the following AWS resources required for streaming config changes to Datadog.

- Config Recorder to record config changes
- Delivery Channel to store configs with 
    - SNS Topic + Subscription 
    - S3 Bucket
- IAM Role + Policy for the Recorder which allow to capture config changes for all resource types and push them to S3 / SNS.
- Firehose stream subscribed to the SNS topic via the created subscription which streams config changes to a predefined DataDog endpoint.
- S3 bucket to store configs which failed to be sent via the Firehose stream.
- IAM Role + Policy for the Firehose stream needed to access the failed configs S3 bucket and subscribe to the SNS topic.

## Publishing the template
Use the release script to upload the template to a S3 bucket following the example below. Make sure you have correct access credentials before launching the script.

```
./release <bucket_name>
```

Use an optional argument `--private` to prevent granting public access to the uploaded template (good for testing purposes). 
The uploaded template file can be found at `/aws/main_config_stream.yaml` key on the chosen S3 bucket.



