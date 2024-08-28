# Datadog AWS Cloud Cost Setup

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-cloud-cost-setup&templateURL=https://datadog-cloudformation-template.s3.amazonaws.com/aws/datadog_cloud_cost_setup.yaml)

## AWS Resources

This template creates the following AWS resources required for setting up Cloud Cost Management in Datadog.

- An S3 Bucket (if not using an existing one)
    - With Bucket policies
- Cost and Usage Report using the COST_AND_USAGE_REPORT table configurations
- IAM policy needed to access the bucket and the CUR
    - Attaches the policy to the main Datadog integration role

## Publishing the template
Use the release script to upload the template to a S3 bucket following the example below. Make sure you have correct access credentials before launching the script.

```
./release <bucket_name>
```

Use an optional argument `--private` to prevent granting public access to the uploaded template (good for testing purposes). 
The uploaded template file can be found at `/aws/datadog_cloud_cost_setup.yaml` key on the chosen S3 bucket.



