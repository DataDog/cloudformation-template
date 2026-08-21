def physical_resource_id(event):
    return event.get("PhysicalResourceId") or (
        f"{event['StackId']}/{event['LogicalResourceId']}"
    )


def send_cfn_response(cfn_response, event, context, status, response_data):
    cfn_response.send(
        event,
        context,
        status,
        responseData=response_data,
        physicalResourceId=physical_resource_id(event),
    )
