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
API_CALL_SOURCE_HEADER_VALUE = "cfn-organizations"
# The "-v2" suffix on these policy names is load-bearing, not cosmetic. The pre-extraction
# inline trigger (<= v4.13) deletes policies by their un-suffixed names on teardown, and that
# teardown runs whenever the old trigger is removed — i.e. when a role stack is upgraded off
# <= v4.13. Distinct v2 names ensure that destructive delete can never hit the policies this
# template attaches:
#   - standard / resource-collection: an in-place role-stack upgrade removes the old trigger
#     after this nested stack has re-attached them; v2 names keep them from being wiped.
#   - instrumentation: the add-on attaches these against an existing role; if that role's stack
#     is later upgraded off <= v4.13, the old trigger's unconditional instrumentation cleanup
#     would wipe them unless they sit under a name it doesn't know.
POLICY_NAME_STANDARD = "DatadogAWSIntegrationPolicyV2"
BASE_POLICY_PREFIX_RESOURCE_COLLECTION = "datadog-aws-integration-resource-collection-permissions-v2"
BASE_POLICY_PREFIX_INSTRUMENTATION = "datadog-aws-integration-instrumentation-permissions-v2"
# Un-suffixed standard/resource-collection names created by the pre-extraction inline trigger
# (<= v4.13). The role-creation path cleans these up before attaching the v2 policies so the two
# generations never sit attached at once (IAM caps managed policies per role, default 10); the
# old trigger's own Delete handler then no-ops against names that are already gone. Legacy
# instrumentation policies need no such cleanup — that feature is unreleased, so none exist.
LEGACY_POLICY_NAME_STANDARD = "DatadogAWSIntegrationPolicy"
LEGACY_PREFIX_RESOURCE_COLLECTION = "datadog-aws-integration-resource-collection-permissions"
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


def _cleanup_chunked_policies(iam_client, role_name, account_id, partition, prefix, max_policies=10):
    for i in range(max_policies):
        policy_name = f"{prefix}-{role_name}-{i+1}"
        policy_arn = f"arn:{partition}:iam::{account_id}:policy/{policy_name}"
        _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name)


def _cleanup_base_policies(iam_client, role_name, account_id, partition, rc_prefix, standard_name, max_policies=10):
    _cleanup_chunked_policies(iam_client, role_name, account_id, partition, rc_prefix, max_policies)
    try:
        iam_client.delete_role_policy(RoleName=role_name, PolicyName=standard_name)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        LOGGER.error(f"Error deleting inline policy {standard_name}: {str(e)}")


def cleanup_existing_policies(iam_client, role_name, account_id, partition, max_policies=10):
    _cleanup_base_policies(iam_client, role_name, account_id, partition, BASE_POLICY_PREFIX_RESOURCE_COLLECTION, POLICY_NAME_STANDARD, max_policies)


def cleanup_instrumentation_policies(iam_client, role_name, account_id, partition, max_policies=10):
    _cleanup_chunked_policies(iam_client, role_name, account_id, partition, BASE_POLICY_PREFIX_INSTRUMENTATION, max_policies)


def cleanup_legacy_base_policies(iam_client, role_name, account_id, partition, max_policies=10):
    # Remove the un-suffixed standard + resource-collection policies left by the pre-extraction
    # inline trigger before the v2 policies are attached, so the two generations don't pile up
    # against the IAM managed-policy limit during an in-place upgrade. Only the role-creation path
    # calls this; the add-on must not touch the policies the role stack owns.
    _cleanup_base_policies(iam_client, role_name, account_id, partition, LEGACY_PREFIX_RESOURCE_COLLECTION, LEGACY_POLICY_NAME_STANDARD, max_policies)


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


def attach_instrumentation_permissions(iam_client, role_name, account_id, partition, datadog_site, resource_types, previous_resource_types, fail_on_error=False):
    # Best-effort by default: instrumentation permissions are additive convenience on top of the
    # integration, so any failure is logged and swallowed rather than blocking install. The
    # post-setup add-on passes fail_on_error=True because attaching these policies is the stack's
    # whole purpose, so a silent SUCCESS that attached nothing would be worse than a visible failure.
    # Fetch before cleanup so that a transient API failure on an Update leaves the
    # previously-attached policies in place instead of silently revoking them.
    if not resource_types:
        # Only clean up if the previous Update had instrumentation enabled — avoids running
        # delete calls on stacks that never opted in to instrumentation in the first place.
        if previous_resource_types:
            cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
        return

    try:
        url = build_instrumentation_permissions_url(datadog_site, resource_types)
        LOGGER.info(f"Fetching instrumentation permissions for {resource_types} from {url}")
        permission_chunks = fetch_permissions_from_datadog(url)
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.warning(
            f"Failed to fetch instrumentation permissions for {resource_types}: {e}. "
            "Leaving any previously-attached instrumentation policies in place."
        )
        return

    cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
    for i, chunk in enumerate(permission_chunks):
        policy_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{i+1}"
        try:
            _create_and_attach_policy(iam_client, role_name, policy_name, chunk)
        except Exception as e:
            if fail_on_error:
                raise
            LOGGER.warning(f"Failed to create/attach instrumentation policy {policy_name}: {e}. Continuing.")


def handle_delete(event, context):
    props = event['ResourceProperties']
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    manage_base_permissions = str(props.get('ManageBasePermissions', 'true')).lower() == 'true'
    iam_client = boto3.client('iam')
    try:
        if manage_base_permissions:
            cleanup_existing_policies(iam_client, role_name, account_id, partition)
        cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, responseData={})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        cfnresponse.send(event, context, cfnresponse.FAILED, responseData={"Message": str(e)})


def handle_create_update(event, context):
    props = event['ResourceProperties']
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    manage_base_permissions = str(props.get('ManageBasePermissions', 'true')).lower() == 'true'
    fail_on_instrumentation_error = str(props.get('FailOnInstrumentationError', 'false')).lower() == 'true'
    should_install_security_audit_policy = str(props['ResourceCollectionPermissions']).lower() == 'true'
    datadog_site = props.get('DatadogSite') or 'datadoghq.com'
    instrumentation_resource_types = parse_resource_types(props.get('InstrumentationResourceTypes'))
    previous_instrumentation_resource_types = parse_resource_types(
        event.get('OldResourceProperties', {}).get('InstrumentationResourceTypes')
    )

    try:
        iam_client = boto3.client('iam')
        if manage_base_permissions:
            cleanup_legacy_base_policies(iam_client, role_name, account_id, partition)
            cleanup_existing_policies(iam_client, role_name, account_id, partition)
            attach_standard_permissions(iam_client, role_name)
            if should_install_security_audit_policy:
                attach_resource_collection_permissions(iam_client, role_name)
        attach_instrumentation_permissions(
            iam_client, role_name, account_id, partition,
            datadog_site, instrumentation_resource_types, previous_instrumentation_resource_types,
            fail_on_error=fail_on_instrumentation_error,
        )
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
