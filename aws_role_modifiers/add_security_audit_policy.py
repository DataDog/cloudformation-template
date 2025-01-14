import json
import logging
import signal
import cfnresponse
import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

class TimeoutError(Exception):
    """Exception for timeouts"""
    pass

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
        cfResponse = {"Message": "Received unexpected request type: {}.".format(event["RequestType"])}
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
            cfResponse = {"Message": "This resource will take no action for updates or deletes."}
            reason = json.dumps(cfResponse)
            cfnresponse.send(
                event,
                context,
                responseStatus="SUCCESS",
                responseData=cfResponse,
                reason=reason,
            )
        iam = boto3.client('iam')
        role_name = event["ResourceProperties"]['RoleName']

        iam.attach_role_policy(
            RoleName=role_name,
            PolicyArn="arn:{partition}:iam::aws:policy/SecurityAudit".format(partition=event["ResourceProperties"]["Partition"])
        )
        LOGGER.info("Success - Policy added to given role.")
        cfResponse = {"Message": "Datadog AWS Integration {} API request was successful.".format(method)}

        cfnresponse.send(
            event,
            context,
            responseStatus="SUCCESS",
            responseData=cfResponse,
            reason=None,
        )

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

def timeout_handler(_signal, _frame):
    """Handle SIGALRM"""
    raise TimeoutError("Lambda function timeout exceeded - increase the timeout set in the api_call Cloudformation template.")

signal.signal(signal.SIGALRM, timeout_handler)
