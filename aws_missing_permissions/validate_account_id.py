import cfnresponse

def handler(event, context):
    request_type = event['RequestType']
    if request_type == 'Delete':
        # Report success so stack deletion can proceed
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
        return
    datadog_account_id = event['ResourceProperties'].get('DatadogAccountId', '')
    aws_account_id = event['ResourceProperties'].get('AWSAccountId', '')
    if datadog_account_id != aws_account_id:
        cfnresponse.send(
            event,
            context,
            responseStatus=cfnresponse.FAILED,
            responseData={},
            reason="The AWS Account Id in Datadog does not match the AWS Account Id that the stack is running in."
        )
    else:
        cfnresponse.send(
            event,
            context,
            responseStatus=cfnresponse.SUCCESS,
            responseData={}
        )
