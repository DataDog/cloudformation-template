#!/usr/bin/env python3

import json
import logging
import urllib.request
import urllib.error
import cfnresponse

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

API_CALL_SOURCE_HEADER_VALUE = "cfn-ccm-cur2"


def get_datadog_account_uuid(event):
    """Get the Datadog account UUID for this AWS account."""
    api_key = event["ResourceProperties"]["APIKey"]
    app_key = event["ResourceProperties"]["APPKey"]
    api_url = event["ResourceProperties"]["ApiURL"]
    account_id = event["ResourceProperties"]["AccountId"]

    url = f"https://api.{api_url}/api/v2/integration/aws/accounts?aws_account_id={account_id}"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }
    request = urllib.request.Request(url, headers=headers)
    request.get_method = lambda: "GET"
    try:
        response = urllib.request.urlopen(request)
        data = json.loads(response.read())
        if len(data.get("data", [])) == 0:
            return None, "No Datadog integration found for this AWS account"
        if len(data["data"]) > 1:
            return None, "Multiple Datadog integrations found for this AWS account"
        return data["data"][0]["id"], None
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return None, f"Failed to get account: {e.code} - {error_body}"


def create_ccm_config(event, uuid):
    """Create CCM config using the dedicated CCM config endpoint."""
    api_key = event["ResourceProperties"]["APIKey"]
    app_key = event["ResourceProperties"]["APPKey"]
    api_url = event["ResourceProperties"]["ApiURL"]
    bucket_name = event["ResourceProperties"]["BucketName"]
    bucket_region = event["ResourceProperties"]["BucketRegion"]
    report_name = event["ResourceProperties"]["ReportName"]
    report_prefix = event["ResourceProperties"]["ReportPrefix"]

    url = f"https://api.{api_url}/api/v2/integration/aws/accounts/{uuid}/ccm_config"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "data": {
            "type": "account",
            "attributes": {
                "ccm_config": {
                    "data_export_configs": [
                        {
                            "report_name": report_name,
                            "report_prefix": report_prefix,
                            "report_type": "CUR2.0",
                            "bucket_name": bucket_name,
                            "bucket_region": bucket_region,
                        }
                    ]
                }
            }
        }
    }

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        response = urllib.request.urlopen(request)
        return response.getcode(), json.loads(response.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        return e.code, {"error": error_body}


def handler(event, context):
    """Handle Lambda event from AWS CloudFormation."""
    LOGGER.info(f"Received event: {json.dumps(event)}")

    if event["RequestType"] == "Delete":
        # On delete, we don't remove the CCM config - just succeed
        LOGGER.info("Delete request - no action needed for CCM config")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, {"Message": "Delete successful"})
        return

    try:
        # Get the Datadog account UUID
        uuid, error = get_datadog_account_uuid(event)
        if error:
            LOGGER.error(f"Failed to get account UUID: {error}")
            cfnresponse.send(event, context, cfnresponse.FAILED, {"Message": error})
            return

        LOGGER.info(f"Found Datadog account UUID: {uuid}")

        # Create the CCM config using the dedicated endpoint
        status_code, response_data = create_ccm_config(event, uuid)

        if status_code == 200:
            LOGGER.info("Successfully configured CCM data export")
            cfnresponse.send(event, context, cfnresponse.SUCCESS, {
                "Message": "CCM data export configured successfully",
                "AccountUUID": uuid,
            })
        else:
            error_msg = f"API returned {status_code}: {response_data}"
            LOGGER.error(error_msg)
            cfnresponse.send(event, context, cfnresponse.FAILED, {"Message": error_msg})

    except Exception as e:
        LOGGER.exception("Exception during processing")
        cfnresponse.send(event, context, cfnresponse.FAILED, {"Message": str(e)})

