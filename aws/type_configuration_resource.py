import logging
import boto3
import json

import json
import logging
import signal
from urllib.request import build_opener, HTTPHandler, Request
from urllib.parse import urlencode

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)


def handler(event, context):
	'''Handle Lambda event from AWS'''
	try:
		LOGGER.info('REQUEST RECEIVED:\n %s', event)
		LOGGER.info('REQUEST RECEIVED:\n %s', context)
		if event['RequestType'] == 'Create':
			LOGGER.info('Received Create request.')
			resource_type_arn = event['ResourceProperties']['TypeARN']
			secret_name = event['ResourceProperties']['SecretName']
			api_key = event['ResourceProperties']['APIKey']
			app_key = event['ResourceProperties']['APPKey']
			api_url = event['ResourceProperties'].get('ApiURL')
			region = context.invoked_function_arn.split(":")[3]

			if not secret_name:
				credentials_dict = {
					"DatadogCredentials": {
						"ApiKey": api_key,
						"ApplicationKey": app_key,
					}
				}
			else:
				client = boto3.client("secretsmanager", region_name=region)
				secret_json = client.get_secret_value(SecretId=secret_name)
				secret_content = json.loads(secret_json["SecretString"])
				credentials_dict = {
					"DatadogCredentials": {
						"ApiKey": secret_content["ClientRequestToken"]["ApiKey"],
						"ApplicationKey": secret_content["ClientRequestToken"]["AppKey"],
					}
				}

			if api_url is not None:
				credentials_dict["DatadogCredentials"]["ApiURL"] = api_url

			client = boto3.client("cloudformation", region_name=region)
			client.set_type_configuration(
				TypeArn=resource_type_arn,
				Configuration=json.dumps(credentials_dict)
			)

			send_response(event, context, "SUCCESS",
						  {"Message": "Type Configuration set correctly."})
		elif event['RequestType'] == 'Update':
			LOGGER.info('Received Update request.')
			send_response(event, context, "SUCCESS",
						  {"Message": "Update not supported, no operation performed."})
		elif event['RequestType'] == 'Delete':
			LOGGER.info('Received Delete request.')
			send_response(event, context, "SUCCESS",
						  {"Message": "Delete not supported, no operation performed."})
		else:
			LOGGER.info('Failed - received unexpected request.')
			send_response(event, context, "FAILED",
						  {"Message": "Unexpected event received from CloudFormation"})
	except Exception as e: #pylint: disable=W0702
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
