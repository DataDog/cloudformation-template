#!/usr/bin/env python3

import json
import logging
import signal
from urllib.request import build_opener, HTTPHandler, HTTPError, Request
import urllib.parse

LOGGER = logging.getLogger()


def call_datadog_agentless_api(event, method):
    template_version = event["ResourceProperties"]["TemplateVersion"]
    api_key = event["ResourceProperties"]["APIKey"]
    app_key = event["ResourceProperties"]["APPKey"]
    dd_site = event["ResourceProperties"]["DatadogSite"]
    account_id = event["ResourceProperties"]["AccountId"]
    hosts = event["ResourceProperties"]["Hosts"]
    containers = event["ResourceProperties"]["Containers"]
    lambdas = event["ResourceProperties"]["Lambdas"]
    sensitive_data = event["ResourceProperties"]["SensitiveData"]
    # Optional parameters
    launch_template_id = event["ResourceProperties"].get("LaunchTemplateId")
    asg_arn = event["ResourceProperties"].get("AutoScalingGroupArn")
    delegate_role_arn = event["ResourceProperties"].get("DelegateRoleArn")
    instance_role_arn = event["ResourceProperties"].get("InstanceRoleArn")
    instance_profile_arn = event["ResourceProperties"].get("InstanceProfileArn")
    orchestrator_policy_arn = event["ResourceProperties"].get("OrchestratorPolicyArn")
    worker_policy_arn = event["ResourceProperties"].get("WorkerPolicyArn")
    worker_dspm_policy_arn = event["ResourceProperties"].get("WorkerDSPMPolicyArn")

    # Make the url Request
    url = f"https://api.${dd_site}/api/v2/agentless_scanning/accounts/aws"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Call-Source": "cfn-agentless-quick-start",
        "Dd-Installation-Version": template_version,
    }

    if method == "DELETE":
        url = f"${url}/${account_id}"
        request = Request(url, headers=headers)
        request.get_method = lambda: method
        try:
            return urllib.request.urlopen(request)
        except HTTPError as e:
            if e.code != 404:
                raise e
            else:
                return e
    elif method == "POST":
        values = {
            "meta": {
                "installation_mode": "cloudformation",
                "installation_version": template_version,
                "cloudformation_stack_id": event["StackId"],
                "resources": {
                    "launch_template_id": launch_template_id,
                    "asg_arn": asg_arn,
                    "delegate_role_arn": delegate_role_arn,
                    "instance_role_arn": instance_role_arn,
                    "instance_profile_arn": instance_profile_arn,
                    "orchestrator_policy_arn": orchestrator_policy_arn,
                    "worker_policy_arn": worker_policy_arn,
                    "worker_dspm_policy_arn": worker_dspm_policy_arn,
                },
            },
            "data": {
                "id": account_id,
                "type": "aws_scan_options",
                "attributes": {
                    "vuln_containers_os": containers == "true",
                    "vuln_host_os": hosts == "true",
                    "lambda": lambdas == "true",
                    "sensitive_data": sensitive_data == "true",
                },
            },
        }
        data = json.dumps(values)
        data = data.encode("utf-8")  # data should be bytes
        url_account_id = f"${url}/${account_id}"
        if is_agentless_scanning_enabled(url_account_id, headers):
            request = Request(url_account_id, data=data, headers=headers)
            request.get_method = lambda: "PATCH"
        else:
            request = Request(url, data=data, headers=headers)
            request.get_method = lambda: "POST"
        request.add_header("Content-Type", "application/vnd.api+json; charset=utf-8")
        request.add_header("Content-Length", len(data))
        response = urllib.request.urlopen(request)
        return response
    else:
        LOGGER.error("Unsupported HTTP method.")
        return None


def is_agentless_scanning_enabled(url_account_id, headers):
    """Check if agentless scanning is already enabled for the account"""
    try:
        request = Request(url_account_id, headers=headers)
        request.get_method = lambda: "GET"
        response = urllib.request.urlopen(request)
        return response.getcode() == 200
    except HTTPError as e:
        if e.code != 404:
            raise e
        return False


def handler(event, context):
    """Handle Lambda event from AWS"""
    try:
        if event["RequestType"] == "Create":
            LOGGER.info("Received Create request.")
            response = call_datadog_agentless_api(event, "POST")
            if response.getcode() == 201 or response.getcode() == 204:
                send_response(
                    event,
                    context,
                    "SUCCESS",
                    {
                        "Message": "Datadog AWS Agentless Scanning Integration created successfully.",
                    },
                )
            else:
                LOGGER.error("Failed - unexpected status code: %d", response.getcode())
                send_response(
                    event,
                    context,
                    "FAILED",
                    {"Message": "Http response: {}".format(response.msg)},
                )

        elif event["RequestType"] == "Update":
            LOGGER.info("Received Update request.")
            send_response(
                event,
                context,
                "SUCCESS",
                {"Message": "Update not supported, no operation performed."},
            )
        elif event["RequestType"] == "Delete":
            LOGGER.info("Received Delete request.")
            response = call_datadog_agentless_api(event, "DELETE")

            if response.getcode() == 200:
                send_response(
                    event,
                    context,
                    "SUCCESS",
                    {
                        "Message": "Datadog AWS Agentless Scanning Integration deleted successfully.",
                    },
                )
            else:
                LOGGER.error("Failed - unexpected status code: %d", response.getcode())
                send_response(
                    event,
                    context,
                    "FAILED",
                    {"Message": "Http response: {}".format(response.msg)},
                )

        else:
            LOGGER.error(
                "Failed - received unexpected request: %s", event["RequestType"]
            )
            send_response(
                event,
                context,
                "FAILED",
                {"Message": "Unexpected event received from CloudFormation"},
            )
    except Exception as e:  # pylint: disable=W0702
        LOGGER.exception("Failed - exception thrown during processing.")
        send_response(
            event,
            context,
            "FAILED",
            {"Message": "Exception during processing: {}".format(e)},
        )


def send_response(event, context, response_status, response_data):
    """Send a resource manipulation status response to CloudFormation"""
    response_body = json.dumps(
        {
            "Status": response_status,
            "Reason": "See the details in CloudWatch Log Stream: "
            + context.log_stream_name,
            "PhysicalResourceId": event.get(
                "PhysicalResourceId", context.invoked_function_arn
            ),
            "StackId": event["StackId"],
            "RequestId": event["RequestId"],
            "LogicalResourceId": event["LogicalResourceId"],
            "Data": response_data,
        }
    )
    formatted_response = response_body.encode("utf-8")

    LOGGER.info("ResponseURL: %s", event["ResponseURL"])
    LOGGER.info("ResponseBody: %s", response_body)

    opener = build_opener(HTTPHandler)
    request = Request(event["ResponseURL"], data=formatted_response)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Content-Length", len(formatted_response))
    request.get_method = lambda: "PUT"
    response = opener.open(request)
    LOGGER.info("Status code: %s", response.getcode())
    LOGGER.info("Status message: %s", response.msg)


def timeout_handler(_signal, _frame):
    """Handle SIGALRM"""
    raise Exception("Time exceeded")


signal.signal(signal.SIGALRM, timeout_handler)
