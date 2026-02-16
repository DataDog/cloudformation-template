#!/usr/bin/env python3

import json
import logging
import signal
from urllib.request import build_opener, HTTPHandler, HTTPError, Request
import urllib.parse

import boto3

LOGGER = logging.getLogger()


def call_datadog_agentless_api(context, event, method):
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
    scanner_policy_arn = event["ResourceProperties"].get("ScannerPolicyArn")
    orchestrator_policy_arn = event["ResourceProperties"].get("OrchestratorPolicyArn")
    worker_policy_arn = event["ResourceProperties"].get("WorkerPolicyArn")
    worker_dspm_policy_arn = event["ResourceProperties"].get("WorkerDSPMPolicyArn")

    # Make the url Request
    url = f"https://api.{dd_site}/api/v2/agentless_scanning/accounts/aws"
    headers = {
        "DD-API-KEY": api_key,
        "DD-APPLICATION-KEY": app_key,
        "Dd-Call-Source": "cfn-agentless-quick-start",
        "Dd-Installation-Version": template_version,
    }

    if method == "DELETE":
        url = f"{url}/{account_id}"
        request = Request(url, headers=headers, method="DELETE")
        try:
            return urllib.request.urlopen(request)
        except HTTPError as e:
            if e.status < 500:
                # For most client errors, the best option is to continue with the
                # stack deletion, since users have no way to fix the request, and
                # at least this way they can clean up the scanner resources.
                return e
            else:
                raise

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
                    "scanner_policy_arn": scanner_policy_arn,
                    "orchestrator_policy_arn": orchestrator_policy_arn,
                    "worker_policy_arn": worker_policy_arn,
                    "worker_dspm_policy_arn": worker_dspm_policy_arn,
                    "invoked_function_arn": context.invoked_function_arn,
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
        url_account = f"{url}/{account_id}"
        if is_agentless_scanning_enabled(url_account, headers):
            request = Request(url_account, data=data, headers=headers, method="PATCH")
        else:
            request = Request(url, data=data, headers=headers, method="POST")
        request.add_header("Content-Type", "application/vnd.api+json; charset=utf-8")
        request.add_header("Content-Length", len(data))
        response = urllib.request.urlopen(request)
        return response
    else:
        LOGGER.error("Unsupported HTTP method.")
        return None


def is_agentless_scanning_enabled(url_account, headers):
    """Check if agentless scanning is already enabled for the account"""
    request = Request(url_account, headers=headers, method="GET")
    try:
        urllib.request.urlopen(request)
    except HTTPError as e:
        if e.status == 404:
            return False
        else:
            raise
    return True


def ensure_security_audit_policy(role_name, partition):
    """Ensure the SecurityAudit policy is attached to the integration role."""
    if not role_name:
        LOGGER.info("No integration role name provided, skipping SecurityAudit policy attachment.")
        return

    policy_arn = f"arn:{partition}:iam::aws:policy/SecurityAudit"
    iam = boto3.client("iam")

    paginator = iam.get_paginator("list_attached_role_policies")
    for page in paginator.paginate(RoleName=role_name):
        for policy in page["AttachedPolicies"]:
            if policy["PolicyArn"] == policy_arn:
                LOGGER.info("SecurityAudit policy is already attached to role %s.", role_name)
                return

    LOGGER.info("Attaching SecurityAudit policy to role %s.", role_name)
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


def handler(event, context):
    """Handle Lambda event from AWS"""
    try:
        if event["RequestType"] == "Create":
            LOGGER.info("Received Create request.")
            role_name = event["ResourceProperties"].get("IntegrationRoleName", "")
            partition = event["ResourceProperties"].get("Partition", "aws")
            ensure_security_audit_policy(role_name, partition)
            response = call_datadog_agentless_api(context, event, "POST")
            send_response(
                event,
                context,
                "SUCCESS",
                {
                    "Message": f"Datadog Agentless Scanning activated (status: {response.status}).",
                },
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
            response = call_datadog_agentless_api(context, event, "DELETE")
            send_response(
                event,
                context,
                "SUCCESS",
                {
                    "Message": f"Datadog Agentless Scanning deactivated (status: {response.status}).",
                },
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
            {"Message": f"Exception during processing: {e}"},
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
    request = Request(event["ResponseURL"], data=formatted_response, method="PUT")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    request.add_header("Content-Length", len(formatted_response))
    response = opener.open(request)
    LOGGER.info("Status code: %s", response.status)
    LOGGER.info("Status message: %s", response.msg)


def timeout_handler(_signal, _frame):
    """Handle SIGALRM"""
    raise Exception("Time exceeded")


signal.signal(signal.SIGALRM, timeout_handler)
