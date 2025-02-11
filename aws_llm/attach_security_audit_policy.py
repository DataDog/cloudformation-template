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
    # This function will only attach the SecurityAudit policy on a create request, and will do nothing on updates or deletes.
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
        return
    iam = boto3.client('iam')
    role_name = event["ResourceProperties"]['RoleName']

    iam.attach_role_policy(
        RoleName=role_name,
        PolicyArn="arn:{partition}:iam::aws:policy/SecurityAudit".format(partition=event["ResourceProperties"]["Partition"])
    )
    LOGGER.info("Success - Policy added to given role.")
    cfResponse = {"Message": "SecurityAudit policy successfully attached to role."}

    cfnresponse.send(
        event,
        context,
        responseStatus="SUCCESS",
        responseData=cfResponse,
        reason=None,
    )

def timeout_handler(_signal, _frame):
    """Handle SIGALRM"""
    raise TimeoutError("Lambda function timeout exceeded - increase the timeout set in the api_call Cloudformation template.")

signal.signal(signal.SIGALRM, timeout_handler)