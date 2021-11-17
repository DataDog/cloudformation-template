import boto3

import json
import logging
import signal
from urllib.request import build_opener, HTTPHandler, Request

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

TEMPLATE_BODY = """
AWSTemplateFormatVersion: 2010-09-09
Description: Datadog AWS Integration API Call
Parameters:
  RoleName:
    Description: >-
      The name of the IAM role created for Datadog's use.
    Type: String
  HostTags:
    Type: CommaDelimitedList
    Default: ""
    Description: >-
      A comma seperated list of tags to add to hosts and metrics
Resources:
  DatadogAWSAccountIntegration:
    Type: Datadog::Integrations::AWSQuickstart
    Properties:
      AccountID: !Ref AWS::AccountId
      RoleName: !Ref RoleName
      HostTags: !Ref HostTags
"""


def handler(event, context):
    '''Handle Lambda event from AWS'''
    try:
        LOGGER.info('REQUEST RECEIVED:\n %s', event)
        LOGGER.info('REQUEST RECEIVED:\n %s', context)
        if event['RequestType'] == 'Create':
            LOGGER.info('Received Create request.')
            region = event['ResourceProperties']['Region']
            role_name = event['ResourceProperties']['RoleName']
            host_tags = event['ResourceProperties']['HostTags']

            client = boto3.client("cloudformation", region_name=region)
            client.create_stack(
                StackName="DatadogAWSIntegrationAPICall",
                TemplateBody=TEMPLATE_BODY,
                Parameters=[
                    {"ParameterKey": "RoleName", "ParameterValue": role_name},
                    {"ParameterKey": "HostTags", "ParameterValue": host_tags},
                ]
            )
            secretsmanager_client = boto3.client("secretsmanager", region_name=region)
            secret_json = secretsmanager_client.get_secret_value(
                SecretId="DatadogIntegrationExternalID"
            )["SecretString"]

            send_response(event, context, "SUCCESS",
                          {"Message": "Type Configuration set correctly.",
                           "ExternalID": json.loads(secret_json)["external_id"]})
        elif event['RequestType'] == 'Update':
            LOGGER.info('Received Update request.')
            send_response(event, context, "SUCCESS",
                          {"Message": "Update not supported, no operation performed."})
        elif event['RequestType'] == 'Delete':
            LOGGER.info('Received Delete request.')
            region = event['ResourceProperties']['Region']

            client = boto3.client("cloudformation", region_name=region)
            client.delete_stack(
                StackName="DatadogAWSIntegrationAPICall"
            )

            send_response(event, context, "SUCCESS",
                          {"Message": "Delete not supported, no operation performed."})
        else:
            LOGGER.info('Failed - received unexpected request.')
            send_response(event, context, "FAILED",
                          {"Message": "Unexpected event received from CloudFormation"})
    except Exception as e:  # pylint: disable=W0702
        LOGGER.info('Failed - exception thrown during processing.')
        send_response(event, context, "FAILED", {
            "Message": "Exception during processing: {}".format(e)})


def send_response(event, context, response_status, response_data):
    '''Send a resource manipulation status response to CloudFormation'''
    response_body = json.dumps({
        "Status": response_status,
        "Reason": "See the details in CloudWatch Log Stream: " + context.log_stream_name,
        "PhysicalResourceId": context.log_stream_name,
        "StackId": event['StackId'],
        "RequestId": event['RequestId'],
        "LogicalResourceId": event['LogicalResourceId'],
        "Data": response_data
    })
    formatted_response = response_body.encode("utf-8")

    LOGGER.info('ResponseURL: %s', event['ResponseURL'])
    LOGGER.info('ResponseBody: %s', response_body)

    opener = build_opener(HTTPHandler)
    request = Request(event['ResponseURL'], data=formatted_response)
    request.add_header('Content-Type', 'application/json; charset=utf-8')
    request.add_header('Content-Length', len(formatted_response))
    request.get_method = lambda: 'PUT'
    response = opener.open(request)
    LOGGER.info("Status code: %s", response.getcode())
    LOGGER.info("Status message: %s", response.msg)


def timeout_handler(_signal, _frame):
    '''Handle SIGALRM'''
    raise Exception('Time exceeded')


signal.signal(signal.SIGALRM, timeout_handler)
