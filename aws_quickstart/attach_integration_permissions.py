import json
import logging
from urllib.request import Request
import urllib.error
import urllib.parse
import urllib.request
import cfnresponse
import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)
API_CALL_SOURCE_HEADER_VALUE = "cfn-quickstart"
POLICY_NAME_STANDARD = "DatadogAWSIntegrationPolicy"
BASE_POLICY_PREFIX_RESOURCE_COLLECTION = "datadog-aws-integration-resource-collection-permissions"
BASE_POLICY_PREFIX_INSTRUMENTATION = "datadog-aws-integration-instrumentation-permissions"
STANDARD_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/standard"
RESOURCE_COLLECTION_PERMISSIONS_API_URL = "https://api.datadoghq.com/api/v2/integration/aws/iam_permissions/resource_collection?chunked=true"
INSTRUMENTATION_PERMISSIONS_API_PATH = "/api/unstable/instrumenter/aws/iam_permissions"

class DatadogAPIError(Exception):
    pass

def fetch_permissions_from_datadog(api_url):
    """Fetch permissions from Datadog API"""
    headers = {
        "Dd-Aws-Api-Call-Source": API_CALL_SOURCE_HEADER_VALUE,
    }
    request = Request(api_url, headers=headers)
    request.get_method = lambda: "GET"

    try:
        response = urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        error_body = json.loads(e.read())
        error_message = error_body.get('errors', ['Unknown error'])[0]
        raise DatadogAPIError(f"Datadog API error: {error_message}") from e

    json_response = json.loads(response.read())
    return json_response["data"]["attributes"]["permissions"]

def parse_resource_types(raw):
    """Parse the InstrumentationResourceTypes ResourceProperty into a clean list of UDM strings.
    Accepts a CFN comma-delimited string or an already-split list (CFN serializes
    CommaDelimitedList parameters as JSON arrays when forwarded to a custom resource)."""
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [t.strip() for t in items if t and t.strip()]

def build_instrumentation_permissions_url(datadog_site, resource_types):
    site = datadog_site or "datadoghq.com"
    query = urllib.parse.urlencode(
        [("resource_type", t) for t in resource_types] + [("chunked", "true")]
    )
    return f"https://api.{site}{INSTRUMENTATION_PERMISSIONS_API_PATH}?{query}"

def _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name):
    """Detach a managed policy from a role and delete it. Ignores missing entities."""
    try:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        LOGGER.error(f"Error detaching policy {policy_name}: {str(e)}")

    try:
        iam_client.delete_policy(PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except iam_client.exceptions.DeleteConflictException:
        LOGGER.warning(f"Policy {policy_name} still attached, skipping delete")
    except Exception as e:
        LOGGER.error(f"Error deleting policy {policy_name}: {str(e)}")

def cleanup_existing_policies(iam_client, role_name, account_id, partition, max_policies=10):
    # Remove role-scoped chunked policies for both prefixes
    for prefix in (BASE_POLICY_PREFIX_RESOURCE_COLLECTION, BASE_POLICY_PREFIX_INSTRUMENTATION):
        for i in range(max_policies):
            policy_name = f"{prefix}-{role_name}-{i+1}"
            policy_arn = f"arn:{partition}:iam::{account_id}:policy/{policy_name}"
            _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name)

    # Remove standard permissions
    try:
        iam_client.delete_role_policy(
            RoleName=role_name,
            PolicyName=POLICY_NAME_STANDARD
        )
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        LOGGER.error(f"Error deleting inline policy {POLICY_NAME_STANDARD}: {str(e)}")

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
    permission_chunks = fetch_permissions_from_datadog(RESOURCE_COLLECTION_PERMISSIONS_API_URL)

    # Create and attach new policies
    for i, chunk in enumerate(permission_chunks):
        policy_name = f"{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-{role_name}-{i+1}"
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

def attach_instrumentation_permissions(iam_client, role_name, datadog_site, resource_types):
    """Best-effort attach IAM permissions required to instrument the given UDM resource types.

    Never raises: instrumentation permissions are additive convenience on top of the integration,
    so any failure here surfaces a warning but does not block the install.
    """
    if not resource_types:
        return

    try:
        url = build_instrumentation_permissions_url(datadog_site, resource_types)
        LOGGER.info(f"Fetching instrumentation permissions for {resource_types} from {url}")
        permission_chunks = fetch_permissions_from_datadog(url)
    except Exception as e:
        LOGGER.warning(
            f"Failed to fetch instrumentation permissions for {resource_types}: {e}. "
            "Integration install will continue without these permissions."
        )
        return

    attached, failed = 0, 0
    for i, chunk in enumerate(permission_chunks):
        policy_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{i+1}"
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
        policy_json = json.dumps(policy_document, separators=(',', ':'))
        LOGGER.info(f"Creating policy {policy_name} with {len(chunk)} permissions ({len(policy_json)} characters)")
        try:
            policy = iam_client.create_policy(
                PolicyName=policy_name,
                PolicyDocument=policy_json
            )
            iam_client.attach_role_policy(
                RoleName=role_name,
                PolicyArn=policy['Policy']['Arn']
            )
            attached += 1
        except Exception as e:
            failed += 1
            LOGGER.warning(f"Failed to create/attach instrumentation policy {policy_name}: {e}. Continuing.")

    LOGGER.info(f"Instrumentation permissions: {attached} attached, {failed} failed out of {len(permission_chunks)} chunks")

def handle_delete(event, context, role_name, account_id, partition):
    """Handle stack deletion."""
    iam_client = boto3.client('iam')
    try:
        cleanup_existing_policies(iam_client, role_name, account_id, partition)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handle_create_update(event, context, role_name, account_id, partition, should_install_security_audit_policy, datadog_site, instrumentation_resource_types):
    """Handle stack creation or update."""
    try:
        iam_client = boto3.client('iam')
        cleanup_existing_policies(iam_client, role_name, account_id, partition)
        attach_standard_permissions(iam_client, role_name)
        if should_install_security_audit_policy:
            attach_resource_collection_permissions(iam_client, role_name)
        attach_instrumentation_permissions(iam_client, role_name, datadog_site, instrumentation_resource_types)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})

def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))

    role_name = event['ResourceProperties']['DatadogIntegrationRole']
    account_id = event['ResourceProperties']['AccountId']
    partition = event['ResourceProperties'].get('Partition', 'aws')
    should_install_security_audit_policy = str(event['ResourceProperties']['ResourceCollectionPermissions']).lower() == 'true'
    datadog_site = event['ResourceProperties'].get('DatadogSite', 'datadoghq.com')
    instrumentation_resource_types = parse_resource_types(event['ResourceProperties'].get('InstrumentationResourceTypes'))

    if event['RequestType'] == 'Delete':
        handle_delete(event, context, role_name, account_id, partition)
    else:
        handle_create_update(event, context, role_name, account_id, partition, should_install_security_audit_policy, datadog_site, instrumentation_resource_types)
