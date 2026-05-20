# Datadog AWS Account Automatic Integration

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-account-integration&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws/main_aws_account_integration.yaml)

## AWS Resources

This template creates a stack with a list of resources which captures create account events and integrates them automatically to Datadog.

- `DatadogKeysSecret` a Secret which stores both API and APP keys.
- `DatadogAccountIntegrationTrail` a Cloudtrail trail capturing management events which include create account events.
- `DatadogAccountIntegrationTrailBucket` a S3 bucket to store Cloudtrail events.
- `DatadogAccountIntegrationTrailBucketPolicy` a Policy for the above S3 bucket
- `DatadogAccountIntegrationEventRule` an Eventbridge rule capturing Cloudtrail events to trigger a Lambda function to execute some logic.
- `DatadogAccountIntegrationEventLambdaPermission` an Invocation Permission allowing Eventbridge to trigger Lambda.
- `DatadogAccountIntegrationLambdaFunction` a Lambda function which gets triggered upon receiving Eventbridge events indicating the creation of new accounts.
- `DatadogAccountIntegrationLambdaFunctionRole` a Lambda function role allowing reading secrets and assume role into newly created accounts.

## Publishing the template
Use the release script to upload the template to a S3 bucket following the example below. Make sure you have correct access credentials before launching the script.

```
./release <bucket_name>
```

Use an optional argument `--private` to prevent granting public access to the uploaded template (good for testing purposes). 
The uploaded template file can be found at `/aws/main_aws_account_integration.yaml` key on the chosen S3 bucket.



