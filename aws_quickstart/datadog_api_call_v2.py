import boto3

import json
import logging
import signal
from urllib.request import build_opener, HTTPHandler, Request
import urllib.parse

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

def call_datadog_api(event, method):
    api_key = event['ResourceProperties']['APIKey']
    app_key = event['ResourceProperties']['APPKey']
    api_url = event['ResourceProperties']['ApiURL']
    account_id = event['ResourceProperties']['AccountId']
    role_name = event['ResourceProperties']['RoleName']
    host_tags = event['ResourceProperties']['HostTags']
    cspm = event['ResourceProperties']['CloudSecurityPostureManagement']
    metrics_disabled = event['ResourceProperties']['DisableMetricCollection']

    # Make the url Request
    url = 'https://api.' + api_url + '/api/v1/integration/aws'
    values = {
        'account_id': account_id,
        'role_name': role_name,
    }
    if method != "DELETE":
        values["host_tags"] = host_tags
        values["cspm_resource_collection_enabled"] = cspm == "true"
        values["metrics_collection_enabled"] = metrics_disabled == "false"

    headers = {
        'DD-API-KEY': api_key,
        'DD-APPLICATION-KEY': app_key,
    }
    data = json.dumps(values)
    data = data.encode('utf-8')  # data should be bytes
    request = Request(url, data=data, headers=headers)
    request.add_header('Content-Type', 'application/json; charset=utf-8')
    request.add_header('Content-Length', len(data))
    request.get_method = lambda: method

    # Send the url Request, store external_id
    response = urllib.request.urlopen(request)
    return response

def handler(event, context):
    '''Handle Lambda event from AWS'''
    try:
        LOGGER.info('REQUEST RECEIVED:\n %s', event)
        LOGGER.info('REQUEST RECEIVED:\n %s', context)
        if event['RequestType'] == 'Create':
            LOGGER.info('Received Create request.')
            response = call_datadog_api(event, 'POST')
            if response.getcode() == 200:
                json_response = json.loads(response.read().decode("utf-8"))
                send_response(event, context, "SUCCESS",
                              {
                                  "Message": "Datadog AWS Integration created successfully.",
                                  "ExternalId": json_response["external_id"],
                              })
            else:
                LOGGER.info('Failed - exception thrown during processing.')
                send_response(event, context, "FAILED", {
                    "Message": "Http response: {}".format(response.msg)})

        elif event['RequestType'] == 'Update':
            LOGGER.info('Received Update request.')
            send_response(event, context, "SUCCESS",
                          {"Message": "Update not supported, no operation performed."})
        elif event['RequestType'] == 'Delete':
            LOGGER.info('Received Delete request.')
            response = call_datadog_api(event, 'DELETE')

            if response.getcode() == 200:
                send_response(event, context, "SUCCESS",
                              {
                                  "Message": "Datadog AWS Integration deleted successfully.",
                              })
            else:
                LOGGER.info('Failed - exception thrown during processing.')
                send_response(event, context, "FAILED", {
                    "Message": "Http response: {}".format(response.msg)})

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
