import json
import logging
from urllib.request import Request
import urllib
import cfnresponse
import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
API_CALL_SOURCE_HEADER_VALUE = "cfn-iam-permissions"
MAX_POLICY_SIZE = 6144  # Maximum characters for AWS managed policy document
POLICY_NAME_STANDARD = "DatadogAWSIntegrationPolicy"
BASE_POLICY_PREFIX_RESOURCE_COLLECTION = "datadog-aws-integration-resource-collection-permissions"
STANDARD_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/standard"
RESOURCE_COLLECTION_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/resource_collection"

class DatadogAPIError(Exception):
    pass

def create_chunks(permissions):
    """Create chunks of permissions based on character limit"""
    chunks = []
    current_chunk = []
    
    for permission in permissions:
        # Calculate size if we add this permission
        next_chunk = current_chunk + [permission]
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": next_chunk,
                    "Resource": "*"
                }
            ]
        }
        policy_size = len(json.dumps(policy, separators=(',', ':')))
        
        # If adding this permission would exceed the limit, start a new chunk
        if policy_size > MAX_POLICY_SIZE and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [permission]
        else:
            current_chunk.append(permission)
    
    # Add the last chunk if it has permissions
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def fetch_permissions_from_datadog(api_url):
    """Fetch permissions from Datadog API"""
    headers = {
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }
    request = Request(api_url, headers=headers)
    request.get_method = lambda: "GET"
    
    response = urllib.request.urlopen(request)
    json_response = json.loads(response.read())
    if response.getcode() != 200:
        error_message = json_response.get('errors', ['Unknown error'])[0]
        raise DatadogAPIError(f"Datadog API error: {error_message}")
    
    return json_response["data"]["attributes"]["permissions"]

def cleanup_existing_policies(iam_client, role_name, account_id, max_policies=10):
    """Clean up existing policies"""
    for i in range(max_policies):
        policy_name = f"{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-part{i+1}"
        policy_arn = f"arn:aws:iam::{account_id}:policy/{policy_name}"
        try:
            iam_client.detach_role_policy(
                RoleName=role_name,
                PolicyArn=policy_arn
            )
        except iam_client.exceptions.NoSuchEntityException:
            # Policy to detach doesn't exist
            pass
        except Exception as e:
            LOGGER.error(f"Error detaching policy {policy_name}: {str(e)}")

        try:
            iam_client.delete_policy(
                PolicyArn=policy_arn
            )
        except (iam_client.exceptions.NoSuchEntityException, iam_client.exceptions.DeleteConflictException):
            # Policy to delete doesn't exist
            pass
        except Exception as e:
            LOGGER.error(f"Error deleting policy {policy_name}: {str(e)}")
    
def attach_standard_permissions(iam_client, role_name):
    permissions = fetch_permissions_from_datadog(STANDARD_PERMISSIONS_API_URL)
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": permissions,
                "Resource": "*"
            }
        ]
    }

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=POLICY_NAME_STANDARD,
        PolicyDocument=json.dumps(policy_document, separators=(',', ':'))
    )
    

def attach_resource_collection_permissions(iam_client, role_name):
    # Fetch and smart chunk permissions based on character limits
    permissions = fetch_permissions_from_datadog(RESOURCE_COLLECTION_PERMISSIONS_API_URL)
    permission_chunks = create_chunks(permissions)
    
    LOGGER.info(f"Created {len(permission_chunks)} policy chunks from {len(permissions)} permissions")
    
    # Create and attach new policies
    for i, chunk in enumerate(permission_chunks):
        # Create policy
        policy_name = f"{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-part{i+1}"
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
        
        # Verify policy size before creating
        policy_json = json.dumps(policy_document, separators=(',', ':'))
        policy_size = len(policy_json)
        LOGGER.info(f"Creating policy {policy_name} with {len(chunk)} permissions ({policy_size} characters)")
        
        policy = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument=policy_json
        )
        
        # Attach policy to role
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy['Policy']['Arn']
        )

def handle_delete(event, context, role_name, account_id):
    """Handle stack deletion."""
    iam_client = boto3.client('iam')
    try:
        cleanup_existing_policies(iam_client, role_name, account_id)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handle_create_update(event, context, role_name, account_id):
    """Handle stack creation or update."""
    try:
        iam_client = boto3.client('iam')
        cleanup_existing_policies(iam_client, role_name, account_id)
        attach_standard_permissions(iam_client, role_name)
        attach_resource_collection_permissions(iam_client, role_name)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))
    
    role_name = event['ResourceProperties']['DatadogIntegrationRole']
    account_id = event['ResourceProperties']['AccountId']
    
    if event['RequestType'] == 'Delete':
        handle_delete(event, context, role_name, account_id)
    else:
        handle_create_update(event, context, role_name, account_id)
