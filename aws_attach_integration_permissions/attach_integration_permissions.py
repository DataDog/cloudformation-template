import json
import logging
import hashlib
from urllib.request import Request
import urllib
import cfnresponse
import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
API_CALL_SOURCE_HEADER_VALUE = "cfn-iam-permissions"
CHUNK_SIZE = 150  # Maximum number of IAM permissions per customer managed policy
BASE_POLICY_PREFIX = "datadog-aws-integration-iam-permissions"

def generate_policy_hash(role_name, account_id):
    """Generate a unique hash for policy naming."""
    unique_string = f"{role_name}-{account_id}"
    return hashlib.md5(unique_string.encode()).hexdigest()[:8]

def get_policy_arn(account_id, policy_name):
    """Generate a policy ARN."""
    return f"arn:aws:iam::{account_id}:policy/{policy_name}"

def fetch_permissions_from_datadog():
    """Fetch permissions from Datadog API."""
    api_url = f"https://api.datadoghq.com/api/v2/integration/aws/iam_permissions"
    headers = {
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }
    request = Request(api_url, headers=headers)
    request.get_method = lambda: "GET"
    
    response = urllib.request.urlopen(request)
    if response.getcode() != 200:
        raise Exception("Failed to fetch permissions from Datadog API")
    
    json_response = json.loads(response.read())
    return json_response["data"]["attributes"]["permissions"]

def cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name, max_policies=10):
    """Clean up existing policies with the base_policy_name prefix"""
    for i in range(max_policies):
        policy_name = f"{base_policy_name}-part{i+1}"
        policy_arn = get_policy_arn(account_id, policy_name)
        try:
            iam_client.detach_role_policy(
                RoleName=role_name,
                PolicyArn=policy_arn
            )
        except iam_client.exceptions.NoSuchEntityException:
            # Policy to detach doesn't exist
            continue
        except Exception as e:
            LOGGER.error(f"Error detaching policy {policy_name}: {str(e)}")

        try:
            iam_client.delete_policy(
                PolicyArn=policy_arn
            )
        except iam_client.exceptions.NoSuchEntityException:
            # Policy to delete doesn't exist
            continue
        except Exception as e:
            LOGGER.error(f"Error deleting policy {policy_name}: {str(e)}")

def handle_delete(event, context, role_name, account_id, base_policy_name):
    """Handle stack deletion."""
    iam_client = boto3.client('iam')
    try:
        cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handle_create_update(event, context, role_name, account_id, base_policy_name):
    """Handle stack creation or update."""
    try:
        # Fetch and chunk permissions
        permissions = fetch_permissions_from_datadog()
        permission_chunks = [permissions[i:i + CHUNK_SIZE] for i in range(0, len(permissions), CHUNK_SIZE)]
        
        # Clean up existing policies
        iam_client = boto3.client('iam')
        cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name)

        # Create and attach new policies
        for i, chunk in enumerate(permission_chunks):
            # Create policy
            policy_name = f"{base_policy_name}-part{i+1}"
            policy_document = {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": chunk,
                        "Resource": "*"
                    }
                ]
            }
            policy = iam_client.create_policy(
                PolicyName=policy_name,
                PolicyDocument=json.dumps(policy_document)
            )
            
            # Attach policy to role
            iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn=policy['Policy']['Arn']
            )
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))
    
    role_name = event['ResourceProperties']['DatadogIntegrationRole']
    account_id = event['ResourceProperties']['AccountId']
    unique_hash = generate_policy_hash(role_name, account_id)
    base_policy_name = f"{BASE_POLICY_PREFIX}-{unique_hash}"
    
    if event['RequestType'] == 'Delete':
        handle_delete(event, context, role_name, account_id, base_policy_name)
    else:
        handle_create_update(event, context, role_name, account_id, base_policy_name)
