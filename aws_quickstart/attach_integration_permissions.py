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
MAX_POLICY_SIZE = 6144  # Maximum characters for AWS managed policy document
BASE_POLICY_PREFIX = "datadog-aws-integration-iam-permissions"
STANDARD_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/standard"
RESOURCE_COLLECTION_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/resource_collection"

class DatadogAPIError(Exception):
    pass

def get_policy_arn(account_id, policy_name):
    """Generate a policy ARN."""
    return f"arn:aws:iam::{account_id}:policy/{policy_name}"

def create_smart_chunks(permissions):
    """Create chunks based on character limit rather than permission count."""
    chunks = []
    current_chunk = []
    
    # Base policy structure size (without permissions)
    base_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [],
                "Resource": "*"
            }
        ]
    }
    base_size = len(json.dumps(base_policy, separators=(',', ':')))
    
    for permission in permissions:
        # Calculate size if we add this permission
        test_chunk = current_chunk + [permission]
        test_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": test_chunk,
                    "Resource": "*"
                }
            ]
        }
        test_size = len(json.dumps(test_policy, separators=(',', ':')))
        
        # If adding this permission would exceed the limit, start a new chunk
        if test_size > MAX_POLICY_SIZE and current_chunk:
            chunks.append(current_chunk)
            current_chunk = [permission]
            current_size = len(json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [permission],
                        "Resource": "*"
                    }
                ]
            }, separators=(',', ':')))
        else:
            current_chunk.append(permission)
            current_size = test_size
    
    # Add the last chunk if it has permissions
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

def fetch_permissions_from_datadog(api_url):
    """Fetch permissions from Datadog API."""
    # api_url = f"https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/standard"
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

def cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name, max_policies=20):
    """Clean up existing policies with both old and new naming patterns"""
    
    # Clean up new dynamic policies (datadog-aws-integration-iam-permissions-part{N})
    for i in range(max_policies):
        policy_name = f"{BASE_POLICY_PREFIX}-part{i+1}"
        policy_arn = get_policy_arn(account_id, policy_name)
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
    
    # Clean up old hardcoded managed policies ({IAMRoleName}-ManagedPolicy-{N})
    # Extract role name from the base_policy_name for old policy cleanup
    role_name_from_hash = role_name  # We have the role name directly
    for i in range(1, 5):  # Old template had ManagedPolicy-1 through ManagedPolicy-4
        old_policy_name = f"{role_name_from_hash}-ManagedPolicy-{i}"
        old_policy_arn = get_policy_arn(account_id, old_policy_name)
        try:
            iam_client.detach_role_policy(
                RoleName=role_name,
                PolicyArn=old_policy_arn
            )
            LOGGER.info(f"Detached old policy: {old_policy_name}")
        except iam_client.exceptions.NoSuchEntityException:
            # Policy to detach doesn't exist
            pass
        except Exception as e:
            LOGGER.error(f"Error detaching old policy {old_policy_name}: {str(e)}")

        try:
            iam_client.delete_policy(
                PolicyArn=old_policy_arn
            )
            LOGGER.info(f"Deleted old policy: {old_policy_name}")
        except (iam_client.exceptions.NoSuchEntityException, iam_client.exceptions.DeleteConflictException):
            # Policy to delete doesn't exist
            pass
        except Exception as e:
            LOGGER.error(f"Error deleting old policy {old_policy_name}: {str(e)}")

def attach_standard_permissions(iam_client, role_name):
    # Fetch permissions
    permissions = fetch_permissions_from_datadog(STANDARD_PERMISSIONS_API_URL)
    # policy_name = f"{BASE_POLICY_PREFIX}-part{i+1}"
    policy_name = "DatadogAWSIntegrationPolicy"
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

    # policy = iam_client.create_policy(
    #     PolicyName=policy_name,
    #     PolicyDocument=json.dumps(policy_document, separators=(',', ':'))
    # )
    # Attach policy to role
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=policy_name,
        PolicyDOcument=json.dumps(policy_document, separators=(',', ':'))
    )
    

def attach_resource_collection_permissions(iam_client):
    # Fetch and smart chunk permissions based on character limits
    permissions = fetch_permissions_from_datadog(RESOURCE_COLLECTION_PERMISSIONS_API_URL)
    permission_chunks = create_smart_chunks(permissions)
    
    LOGGER.info(f"Created {len(permission_chunks)} policy chunks from {len(permissions)} permissions")
    
    # Clean up existing policies
    iam_client = boto3.client('iam')
    # cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name)

    # Create and attach new policies
    for i, chunk in enumerate(permission_chunks):
        # Create policy
        policy_name = f"{BASE_POLICY_PREFIX}-part{i+1}"
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
        
        if policy_size > MAX_POLICY_SIZE:
            LOGGER.error(f"Policy {policy_name} exceeds size limit: {policy_size} > {MAX_POLICY_SIZE}")
            raise Exception(f"Policy size exceeds AWS limit: {policy_size} > {MAX_POLICY_SIZE}")
        
        policy = iam_client.create_policy(
            PolicyName=policy_name,
            PolicyDocument=policy_json
        )
        
        # Attach policy to role
        iam_client.attach_role_policy(
            RoleName=role_name,
            PolicyArn=policy['Policy']['Arn']
        )

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
        iam_client = boto3.client('iam')
        cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name)
        attach_standard_permissions(iam_client)
        attach_resource_collection_permissions(iam_client)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})
    # try:
    #     # Fetch and smart chunk permissions based on character limits
    #     permissions = fetch_permissions_from_datadog(RESOURCE_COLLECTION_PERMISSIONS_API_URL)
    #     permission_chunks = create_smart_chunks(permissions)
        
    #     LOGGER.info(f"Created {len(permission_chunks)} policy chunks from {len(permissions)} permissions")
        
    #     # Clean up existing policies
    #     iam_client = boto3.client('iam')
    #     cleanup_existing_policies(iam_client, role_name, account_id, base_policy_name)

    #     # Create and attach new policies
    #     for i, chunk in enumerate(permission_chunks):
    #         # Create policy
    #         policy_name = f"{BASE_POLICY_PREFIX}-part{i+1}"
    #         policy_document = {
    #             "Version": "2012-10-17",
    #             "Statement": [
    #                 {
    #                     "Effect": "Allow",
    #                     "Action": chunk,
    #                     "Resource": "*"
    #                 }
    #             ]
    #         }
            
    #         # Verify policy size before creating
    #         policy_json = json.dumps(policy_document, separators=(',', ':'))
    #         policy_size = len(policy_json)
    #         LOGGER.info(f"Creating policy {policy_name} with {len(chunk)} permissions ({policy_size} characters)")
            
    #         if policy_size > MAX_POLICY_SIZE:
    #             LOGGER.error(f"Policy {policy_name} exceeds size limit: {policy_size} > {MAX_POLICY_SIZE}")
    #             raise Exception(f"Policy size exceeds AWS limit: {policy_size} > {MAX_POLICY_SIZE}")
            
    #         policy = iam_client.create_policy(
    #             PolicyName=policy_name,
    #             PolicyDocument=policy_json
    #         )
            
    #         # Attach policy to role
    #         iam_client.attach_role_policy(
    #             RoleName=role_name,
    #             PolicyArn=policy['Policy']['Arn']
    #         )
        
    #     cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    # except Exception as e:
    #     LOGGER.error(f"Error creating/attaching policy: {str(e)}")
    #     cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))
    
    role_name = event['ResourceProperties']['DatadogIntegrationRole']
    account_id = event['ResourceProperties']['AccountId']
    
    if event['RequestType'] == 'Delete':
        handle_delete(event, context, role_name, account_id, BASE_POLICY_PREFIX)
    else:
        handle_create_update(event, context, role_name, account_id, BASE_POLICY_PREFIX)
