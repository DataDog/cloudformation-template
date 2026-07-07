# Datadog AWS Cloud Cost Setup

## Templates

- **`main.yaml`** — legacy Cost and Usage Report (CUR), via `AWS::CUR::ReportDefinition`.
- **`cur2-main.yaml`** — CUR 2.0 / BCM Data Exports, via `AWS::BCMDataExports::Export`.

## AWS Resources

Both templates create the following AWS resources required for setting up Cloud Cost Management in Datadog.

- An S3 Bucket (if not using an existing one)
  - With Bucket policies
- A cost report (if not using an existing one)
  - `main.yaml`: a Cost and Usage Report
  - `cur2-main.yaml`: a CUR 2.0 export (BCM Data Export)
- IAM policy needed to access the bucket and the cost report
  - `cur2-main.yaml` additionally grants `bcm-data-exports:GetExport` and `bcm-data-exports:ListExports`
  - Attaches the policy to the main Datadog integration role

## Publishing the template

Use the release script to upload the template to a S3 bucket following the example below. Make sure you have correct access credentials before launching the script.

```sh
./release <bucket_name>
```

Use an optional argument `--private` to prevent granting public access to the uploaded template (good for testing purposes).
The uploaded template file can be found at `/aws_cloud_cost/main.yaml` key on the chosen S3 bucket.
