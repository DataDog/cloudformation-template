provider "aws" {
  region = "us-east-1"
}

# Find your api key from https://app.datadoghq.com/account/settings#api
# Run `export TF_VAR_dd_api_key=<ACTUAL_DD_API_KEY>` to set its value.
variable "dd_api_key" {
  type        = string
  description = "Datadog API key"
}

# Generate an external id from the AWS integration configuration page
# https://app.datadoghq.com/account/settings#integrations/amazon-web-services
# Run `export TF_VAR_external_id=<ACTUAL_EXTERNAL_ID>` to set its value.
variable "external_id" {
  type        = string
  description = "External Id for Datadog Integration IAM role"
}

variable "dd_forwarder_name" {
  type        = string
  default     = "datadog-forwarder"
  description = "Name for Datadog Forwarder Lambda function"
}

# A wrapper on top of the provided CloudFormation stack
# https://www.terraform.io/docs/providers/aws/r/cloudformation_stack.html
resource "aws_cloudformation_stack" "datadog-aws-integration" {
  name         = "datadog-aws-integration"
  capabilities = ["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
  parameters = {
    ExternalId      = var.external_id
    DdApiKey        = var.dd_api_key
    DdForwarderName = var.dd_forwarder_name
  }
  template_url = "https://datadog-cloudformation-template.s3.amazonaws.com/aws/main.yaml"
}