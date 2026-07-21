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
MAX_POLICY_DOCUMENTS = 10
INSTRUMENTATION_UPDATE_PROPERTIES = (
    "PolicyAttachmentSchemaVersion",
    "DatadogIntegrationRole",
    "AccountId",
    "Partition",
    "InstrumentationResourceTypes",
    "DatadogSite",
)


class DatadogAPIError(Exception):
    pass


def fetch_permissions_attributes_from_datadog(api_url):
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

    return json.loads(response.read())["data"]["attributes"]


def fetch_permissions_from_datadog(api_url):
    return fetch_permissions_attributes_from_datadog(api_url)["permissions"]


def fetch_instrumentation_policy_documents(api_url):
    policy_documents = fetch_permissions_attributes_from_datadog(api_url)["policy_documents"]
    if not policy_documents:
        raise DatadogAPIError("Datadog API returned no instrumentation policy documents")
    if len(policy_documents) > MAX_POLICY_DOCUMENTS:
        raise DatadogAPIError(
            f"Datadog API returned {len(policy_documents)} instrumentation policy documents; "
            f"at most {MAX_POLICY_DOCUMENTS} are supported"
        )
    return policy_documents


def parse_resource_types(raw):
    # CFN forwards CommaDelimitedList parameters as JSON arrays to custom resources,
    # while String parameters arrive as comma-delimited strings; accept both.
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [t.strip() for t in items if t and t.strip()]


def _is_enabled(value):
    return str(value).lower() == "true"


def _normalized_instrumentation_property(name, value):
    if name == "InstrumentationResourceTypes":
        return tuple(sorted(set(parse_resource_types(value))))
    if name == "Partition":
        return value or "aws"
    if name == "DatadogSite":
        return value or "datadoghq.com"
    return value


def _should_update_instrumentation_permissions(event):
    if event["RequestType"] != "Update":
        return True

    current = event["ResourceProperties"]
    previous = event["OldResourceProperties"]
    return any(
        _normalized_instrumentation_property(name, current.get(name))
        != _normalized_instrumentation_property(name, previous.get(name))
        for name in INSTRUMENTATION_UPDATE_PROPERTIES
    )


def _physical_resource_id(event):
    return event.get("PhysicalResourceId") or f"{event['StackId']}/{event['LogicalResourceId']}"


def _send_cfn_response(event, context, status, response_data):
    cfnresponse.send(
        event,
        context,
        status,
        responseData=response_data,
        physicalResourceId=_physical_resource_id(event),
    )


def build_instrumentation_permissions_url(datadog_site, resource_types, account_id, partition):
    query = urllib.parse.urlencode(
        [("resource_type", t) for t in resource_types]
        + [
            ("account_id", account_id),
            ("partition", partition),
            ("chunked", "true"),
        ]
    )
    return f"https://api.{datadog_site}{INSTRUMENTATION_PERMISSIONS_API_PATH}?{query}"


def _detach_and_delete_policy(
    iam_client, role_name, policy_arn, policy_name, fail_on_error=False
):
    # Detach + delete are both no-ops if the entity is already gone, so callers can blindly
    # iterate the policy-name space without first checking what actually exists.
    try:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.error(f"Error detaching policy {policy_name}: {str(e)}")

    try:
        iam_client.delete_policy(PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except iam_client.exceptions.DeleteConflictException:
        if fail_on_error:
            raise
        LOGGER.warning(f"Policy {policy_name} still attached, skipping delete")
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.error(f"Error deleting policy {policy_name}: {str(e)}")


def _cleanup_chunked_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    prefix,
    max_policies=10,
    fail_on_error=False,
):
    for i in range(max_policies):
        policy_name = f"{prefix}-{role_name}-{i+1}"
        policy_arn = f"arn:{partition}:iam::{account_id}:policy/{policy_name}"
        _detach_and_delete_policy(
            iam_client,
            role_name,
            policy_arn,
            policy_name,
            fail_on_error=fail_on_error,
        )


def _cleanup_base_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    rc_prefix,
    standard_name,
    max_policies=10,
    fail_on_error=False,
):
    _cleanup_chunked_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        rc_prefix,
        max_policies,
        fail_on_error=fail_on_error,
    )
    try:
        iam_client.delete_role_policy(RoleName=role_name, PolicyName=standard_name)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.error(f"Error deleting inline policy {standard_name}: {str(e)}")


def cleanup_existing_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    max_policies=10,
    fail_on_error=False,
):
    _cleanup_base_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        BASE_POLICY_PREFIX_RESOURCE_COLLECTION,
        POLICY_NAME_STANDARD,
        max_policies,
        fail_on_error=fail_on_error,
    )


def cleanup_instrumentation_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    max_policies=10,
    fail_on_error=False,
):
    _cleanup_chunked_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        BASE_POLICY_PREFIX_INSTRUMENTATION,
        max_policies,
        fail_on_error=fail_on_error,
    )


def cleanup_legacy_base_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    max_policies=10,
    fail_on_error=False,
):
    # Remove the un-suffixed standard + resource-collection policies left by the pre-extraction
    # inline trigger before the v2 policies are attached, so the two generations don't pile up
    # against the IAM managed-policy limit during an in-place upgrade. Only the role-creation path
    # calls this; the add-on must not touch the policies the role stack owns.
    _cleanup_base_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        LEGACY_PREFIX_RESOURCE_COLLECTION,
        LEGACY_POLICY_NAME_STANDARD,
        max_policies,
        fail_on_error=fail_on_error,
    )


def _cleanup_previous_target_policies(iam_client, props):
    role_name = props["DatadogIntegrationRole"]
    account_id = props["AccountId"]
    partition = props.get("Partition", "aws")
    manage_base_permissions = _is_enabled(props.get("ManageBasePermissions", "true"))
    if manage_base_permissions:
        cleanup_legacy_base_policies(
            iam_client,
            role_name,
            account_id,
            partition,
            fail_on_error=True,
        )
        cleanup_existing_policies(
            iam_client,
            role_name,
            account_id,
            partition,
            fail_on_error=True,
        )
    cleanup_instrumentation_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        fail_on_error=True,
    )


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


def _create_and_attach_policy_document(iam_client, role_name, policy_name, policy_document):
    policy_json = json.dumps(policy_document, separators=(',', ':'))
    LOGGER.info(f"Creating policy {policy_name} ({len(policy_json)} characters)")
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


def _list_attached_role_policies(iam_client, role_name):
    policies = []
    marker = None
    while True:
        request = {"RoleName": role_name}
        if marker:
            request["Marker"] = marker
        response = iam_client.list_attached_role_policies(**request)
        policies.extend(response.get("AttachedPolicies", []))
        if not response.get("IsTruncated"):
            return policies
        marker = response["Marker"]


def _is_instrumentation_policy(policy_name, role_name):
    prefix = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-"
    if not policy_name.startswith(prefix):
        return False
    suffix = policy_name[len(prefix):]
    return suffix.isdigit() and 1 <= int(suffix) <= MAX_POLICY_DOCUMENTS


def _validate_instrumentation_policy_capacity(iam_client, role_name, policy_document_count):
    attached_policies = _list_attached_role_policies(iam_client, role_name)
    existing_instrumentation_count = sum(
        _is_instrumentation_policy(policy["PolicyName"], role_name)
        for policy in attached_policies
    )
    non_instrumentation_count = len(attached_policies) - existing_instrumentation_count
    attachment_quota = iam_client.get_account_summary().get("SummaryMap", {}).get(
        "AttachedPoliciesPerRoleQuota"
    )
    if not isinstance(attachment_quota, int) or attachment_quota < 1:
        raise RuntimeError("IAM account summary did not include AttachedPoliciesPerRoleQuota")
    if non_instrumentation_count + policy_document_count > attachment_quota:
        raise RuntimeError(
            f"role {role_name} has room for only "
            f"{attachment_quota - non_instrumentation_count} instrumentation policies, "
            f"but Datadog returned {policy_document_count}"
        )


def attach_instrumentation_permissions(
    iam_client,
    role_name,
    account_id,
    partition,
    datadog_site,
    resource_types,
    previous_resource_types,
    fail_on_error=False,
):
    # Fetch and validate capacity before cleanup so a transient failure leaves existing policies.
    replacing_existing_policies = bool(previous_resource_types)
    mutation_must_succeed = fail_on_error or replacing_existing_policies
    if not resource_types:
        if replacing_existing_policies:
            cleanup_instrumentation_policies(
                iam_client,
                role_name,
                account_id,
                partition,
                fail_on_error=mutation_must_succeed,
            )
        return

    try:
        url = build_instrumentation_permissions_url(
            datadog_site,
            resource_types,
            account_id,
            partition,
        )
        LOGGER.info(f"Fetching instrumentation permissions for {resource_types} from {url}")
        policy_documents = fetch_instrumentation_policy_documents(url)
        _validate_instrumentation_policy_capacity(
            iam_client,
            role_name,
            len(policy_documents),
        )
    except Exception as e:
        if mutation_must_succeed:
            raise
        LOGGER.warning(
            f"Failed to prepare instrumentation permissions for {resource_types}: {e}. "
            "Leaving any previously-attached instrumentation policies in place."
        )
        return

    cleanup_instrumentation_policies(
        iam_client,
        role_name,
        account_id,
        partition,
        fail_on_error=mutation_must_succeed,
    )
    for i, policy_document in enumerate(policy_documents):
        policy_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{i+1}"
        try:
            _create_and_attach_policy_document(
                iam_client,
                role_name,
                policy_name,
                policy_document,
            )
        except Exception as e:
            if mutation_must_succeed:
                raise
            LOGGER.warning(f"Failed to create/attach instrumentation policy {policy_name}: {e}. Continuing.")


def handle_delete(event, context):
    props = event['ResourceProperties']
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    manage_base_permissions = _is_enabled(props.get('ManageBasePermissions', 'true'))
    iam_client = boto3.client('iam')
    try:
        if manage_base_permissions:
            cleanup_existing_policies(iam_client, role_name, account_id, partition)
        cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
        _send_cfn_response(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        _send_cfn_response(event, context, cfnresponse.FAILED, {"Message": str(e)})


def handle_create_update(event, context):
    props = event['ResourceProperties']
    previous_props = event.get('OldResourceProperties', {})
    role_name = props['DatadogIntegrationRole']
    account_id = props['AccountId']
    partition = props.get('Partition', 'aws')
    manage_base_permissions = _is_enabled(props.get('ManageBasePermissions', 'true'))
    fail_on_instrumentation_error = _is_enabled(
        props.get('FailOnInstrumentationError', 'false')
    )
    should_attach_resource_collection_permissions = _is_enabled(
        props['ResourceCollectionPermissions']
    )
    datadog_site = props.get('DatadogSite') or 'datadoghq.com'
    instrumentation_resource_types = parse_resource_types(props.get('InstrumentationResourceTypes'))
    previous_instrumentation_resource_types = parse_resource_types(
        previous_props.get('InstrumentationResourceTypes')
    )
    previous_target = (
        previous_props.get('DatadogIntegrationRole', role_name),
        previous_props.get('AccountId', account_id),
        previous_props.get('Partition', partition),
    )
    current_target = (role_name, account_id, partition)
    target_changed = previous_target != current_target

    try:
        iam_client = boto3.client('iam')
        if manage_base_permissions:
            cleanup_legacy_base_policies(iam_client, role_name, account_id, partition)
            cleanup_existing_policies(iam_client, role_name, account_id, partition)
            attach_standard_permissions(iam_client, role_name)
            if should_attach_resource_collection_permissions:
                attach_resource_collection_permissions(iam_client, role_name)
        if _should_update_instrumentation_permissions(event):
            attach_instrumentation_permissions(
                iam_client,
                role_name,
                account_id,
                partition,
                datadog_site,
                instrumentation_resource_types,
                previous_instrumentation_resource_types,
                fail_on_error=fail_on_instrumentation_error,
            )
        if target_changed:
            _cleanup_previous_target_policies(iam_client, previous_props)
        _send_cfn_response(event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        _send_cfn_response(event, context, cfnresponse.FAILED, {"Message": str(e)})


def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))
    if event['RequestType'] == 'Delete':
        handle_delete(event, context)
    else:
        handle_create_update(event, context)
