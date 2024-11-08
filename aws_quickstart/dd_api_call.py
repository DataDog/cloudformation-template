import json
import logging
import signal
from urllib.request import Request
import urllib.parse
import cfnresponse

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

API_CALL_SOURCE_HEADER_VALUE = "cfn-organizations"


def call_datadog_api(uuid, event, method):
    api_key = event["ResourceProperties"]["APIKey"]
    app_key = event["ResourceProperties"]["APPKey"]
    api_url = event["ResourceProperties"]["ApiURL"]
    account_id = event["ResourceProperties"]["AccountId"]
    role_name = event["ResourceProperties"]["RoleName"]
    aws_partition = event["ResourceProperties"]["AWSPartition"]
    account_tags = event["ResourceProperties"]["AccountTags"]
    cspm = event["ResourceProperties"]["CloudSecurityPostureManagement"]
    metrics_disabled = event["ResourceProperties"]["DisableMetricCollection"]

    # Make the url Request
    url = "https://api." + api_url + "/api/v2/integration/aws/accounts"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }

    if method == "PATCH" or method == "DELETE":
        url = url + "/" + uuid

    if method == "DELETE":
        # DELETE request has no body
        request = Request(url, headers=headers)
    else:
        # Create the request body for POST and PATCH
        values = {
            "data": {
                "type": "account",
                "attributes": {
                    "aws_account_id": account_id,
                    "account_tags": account_tags,
                    "aws_partition": aws_partition,
                    "auth_config": {"role_name": role_name},
                    "metrics_config": {
                        "enabled": (metrics_disabled == "false"),
                    },
                    "resources_config": {
                        "cloud_security_posture_management_collection": (
                            cspm == "true"
                        )
                    }
                }
            }
        }

        data = json.dumps(values)
        data = data.encode("utf-8")  # data should be bytes
        request = Request(url, data=data, headers=headers)
        request.add_header("Content-Type", "application/json; charset=utf-8")
        request.add_header("Content-Length", len(data))

    # Send the request
    request.get_method = lambda: method
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        # Return error response from API
        response = e
    return response

def get_datadog_account(event):
    api_key = event["ResourceProperties"]["APIKey"]
    app_key = event["ResourceProperties"]["APPKey"]
    api_url = event["ResourceProperties"]["ApiURL"]
    account_id = event["ResourceProperties"]["AccountId"]

    # Make the url Request
    url = "https://api." + api_url + "/api/v2/integration/aws/accounts?aws_account_id=" + account_id
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }
    request = Request(url, headers=headers)
    request.get_method = lambda: "GET"
    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        # Return error response from API
        response = e
    return response


def handler(event, context):
    """Handle Lambda event from AWS"""
    if event["RequestType"] == "Create":
        LOGGER.info("Received Create request.")
        method = "POST"

    elif event["RequestType"] == "Update":
        LOGGER.info("Received Update request.")
        method = "PATCH"

    elif event["RequestType"] == "Delete":
        LOGGER.info("Received Delete request.")
        method = "DELETE"
    else:
        LOGGER.info("Failed - received unexpected request.")
        cfResponse = {"Message": "Received unexpected request type: {}".format(event["RequestType"])}
        reason = json.dumps(cfResponse)
        cfnresponse.send(
            event,
            context,
            responseStatus="FAILED",
            responseData=cfResponse,
            reason=reason,
        )
        return

    try:
        # Call Datadog API and report response back to CloudFormation
        uuid = ""
        if event["RequestType"] != "Create":
            datadog_account_response = get_datadog_account(event)
            uuid = extract_uuid_from_account_response(event, context, datadog_account_response)
            if uuid is None:
                return
        response = call_datadog_api(uuid, event, method)
        cfn_response_send_api_result(event, context, method, response)

    except Exception as e:
        LOGGER.info("Failed - exception thrown during processing.")
        cfResponse = {"Message": "Exception during processing: {}".format(e)}
        reason = json.dumps(cfResponse)
        cfnresponse.send(
            event,
            context,
            "FAILED",
            responseData=cfResponse,
            reason=reason,
        )

def extract_uuid_from_account_response(event, context, account_response):
    json_response = ""
    code = account_response.getcode()
    data = account_response.read()
    if data:
        json_response = json.loads(data)
    if code == 200 or code == 204:
        if len(json_response["data"]) == 0:
            cfn_response_send_failure(event, context, "Datadog account not found.")
            return None
        if len(json_response["data"]) > 1:
            cfn_response_send_failure(event, context, "Datadog account not unique.")
            return None
        return json_response["data"][0]["id"]
    else:
        cfn_response_send_failure(event, context, "Datadog API returned error: {}".format(json_response))
        return None


def cfn_response_send_api_result(event, context, method, response):
    reason = None
    json_response = ""
    code = response.getcode()
    data = response.read()
    if data:
        json_response = json.loads(data)
    if code == 200 or code == 204:
        LOGGER.info("Success - Datadog API call was successful.")
        response_status = "SUCCESS"
        cfResponse = {"Message": "Datadog AWS Integration {} API request was successful.".format(method)}

        # return external ID for create and update
        if method == "POST" or method == "PATCH":
            external_id = json_response["data"]["attributes"]["auth_config"]["external_id"]
            cfResponse["ExternalId"] = external_id
        cfnresponse.send(
            event,
            context,
            responseStatus=response_status,
            responseData=cfResponse,
            reason=reason,
        )
        return
    cfn_response_send_failure(event, context, "Datadog API returned error: {}".format(json_response))


def cfn_response_send_failure(event, context, message):
    LOGGER.info("Failed - Datadog API call failed.")
    reason = None
    response_status = "FAILED"
    cfResponse = {"Message": message}
    reason = json.dumps(cfResponse)
    cfnresponse.send(
        event,
        context,
        responseStatus=response_status,
        responseData=cfResponse,
        reason=reason,
    )

def timeout_handler(_signal, _frame):
    """Handle SIGALRM"""
    raise Exception("Time exceeded")


signal.signal(signal.SIGALRM, timeout_handler)
