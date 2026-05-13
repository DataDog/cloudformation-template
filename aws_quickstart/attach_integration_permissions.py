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

    return json.loads(response.read())["data"]["attributes"]["permissions"]


def parse_resource_types(raw):
    # CFN forwards CommaDelimitedList parameters as JSON arrays to custom resources,
    # while String parameters arrive as comma-delimited strings; accept both.
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [t.strip() for t in items if t and t.strip()]


def build_instrumentation_permissions_url(datadog_site, resource_types):
    query = urllib.parse.urlencode(
        [("resource_type", t) for t in resource_types] + [("chunked", "true")]
    )
    return f"https://api.{datadog_site}{INSTRUMENTATION_PERMISSIONS_API_PATH}?{query}"


def _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name):
    # Detach + delete are both no-ops if the entity is already gone, so callers can blindly
    # iterate the policy-name space without first checking what actually exists.
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
    for prefix in (BASE_POLICY_PREFIX_RESOURCE_COLLECTION, BASE_POLICY_PREFIX_INSTRUMENTATION):
        for i in range(max_policies):
            policy_name = f"{prefix}-{role_name}-{i+1}"
            policy_arn = f"arn:{partition}:iam::{account_id}:policy/{policy_name}"
            _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name)

    try:
        iam_client.delete_role_policy(RoleName=role_name, PolicyName=POLICY_NAME_STANDARD)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        LOGGER.error(f"Error deleting inline policy {POLICY_NAME_STANDARD}: {str(e)}")


def attach_standard_permissions(iam_client, role_name):
    permissions = fetch_permissions_from_datadog(STANDARD_PERMISSIONS_API_URL)
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": permissions, "Resource": "*"}],
    }
    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName=POLICY_NAME_STANDARD,
        PolicyDocument=json.dumps(policy_document, separators=(',', ':')),
    )


def _create_and_attach_policy(iam_client, role_name, policy_name, actions):
    policy_json = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": actions, "Resource": "*"}],
        },
        separators=(',', ':'),
    )
    LOGGER.info(f"Creating policy {policy_name} with {len(actions)} permissions ({len(policy_json)} characters)")
    policy = iam_client.create_policy(PolicyName=policy_name, PolicyDocument=policy_json)
    iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy['Policy']['Arn'])


def attach_resource_collection_permissions(iam_client, role_name):
    permission_chunks = fetch_permissions_from_datadog(RESOURCE_COLLECTION_PERMISSIONS_API_URL)
    for i, chunk in enumerate(permission_chunks):
        _create_and_attach_policy(
            iam_client,
            role_name,
            f"{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-{role_name}-{i+1}",
            chunk,
        )


def attach_instrumentation_permissions(iam_client, role_name, datadog_site, resource_types):
    # Best-effort: instrumentation permissions are additive convenience on top of the
    # integration, so any failure here is logged and swallowed rather than blocking install.
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

    for i, chunk in enumerate(permission_chunks):
        policy_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{i+1}"
        try:
            _create_and_attach_policy(iam_client, role_name, policy_name, chunk)
        except Exception as e:
            LOGGER.warning(f"Failed to create/attach instrumentation policy {policy_name}: {e}. Continuing.")


def handle_delete(event, context):
    props = event['ResourceProperties']
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    iam_client = boto3.client('iam')
    try:
        cleanup_existing_policies(iam_client, role_name, account_id, partition)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})


def handle_create_update(event, context):
    props = event['ResourceProperties']
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    should_install_security_audit_policy = str(props['ResourceCollectionPermissions']).lower() == 'true'
    datadog_site = props.get('DatadogSite') or 'datadoghq.com'
    instrumentation_resource_types = parse_resource_types(props.get('InstrumentationResourceTypes'))

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
    if event['RequestType'] == 'Delete':
        handle_delete(event, context)
    else:
        handle_create_update(event, context)
