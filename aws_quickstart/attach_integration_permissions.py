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
# AWS hard limit on a customer managed policy document (non-whitespace characters).
MAX_MANAGED_POLICY_SIZE = 6144
# AWS default managed-policies-per-role quota; matches the cleanup loops' max_policies bound.
MAX_ATTACHED_MANAGED_POLICIES = 10


class DatadogAPIError(Exception):
    pass


class InstrumentationPolicyLimitError(Exception):
    pass


def fetch_attributes_from_datadog(api_url):
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
    return fetch_attributes_from_datadog(api_url)["permissions"]


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


def render_placeholders(value, account_id, partition):
    if isinstance(value, str):
        return value.replace("${AccountId}", account_id).replace("${Partition}", partition)
    if isinstance(value, list):
        return [render_placeholders(v, account_id, partition) for v in value]
    if isinstance(value, dict):
        return {k: render_placeholders(v, account_id, partition) for k, v in value.items()}
    return value


def legacy_chunk_to_statement(chunk):
    return {"Effect": "Allow", "Action": list(chunk), "Resource": "*"}


def resolve_instrumentation_statement_chunks(
    attributes, account_id, partition, standard_statements, max_size=MAX_MANAGED_POLICY_SIZE
):
    # Prefer structured policy_statements when the API provides them; policy_statements is
    # always unchunked, so QuickStart renders, filters, and chunks it by size (Task 5).
    # permissions is pre-chunked server-side for the legacy fallback — filter each chunk but
    # preserve its boundaries rather than re-chunking, since dd-source already sized it to fit.
    policy_statements = attributes.get("policy_statements")
    if policy_statements is not None:
        rendered = [render_placeholders(s, account_id, partition) for s in policy_statements]
        filtered = filter_instrumentation_statements(rendered, standard_statements)
        return chunk_statements(filtered, max_size=max_size)

    legacy_statements = [legacy_chunk_to_statement(chunk) for chunk in attributes.get("permissions", [])]
    filtered = filter_instrumentation_statements(legacy_statements, standard_statements)
    return [[statement] for statement in filtered]


def _resource_covers(standard_resource, instrumentation_resource):
    standard_set = set(standard_resource) if isinstance(standard_resource, list) else {standard_resource}
    if "*" in standard_set:
        return True
    instrumentation_set = (
        set(instrumentation_resource) if isinstance(instrumentation_resource, list) else {instrumentation_resource}
    )
    return instrumentation_set.issubset(standard_set)


def _condition_covers(standard_condition, instrumentation_condition):
    # An absent condition on the standard statement is broader than any condition on the
    # instrumentation side; a present condition must match exactly, otherwise it's not
    # equivalent-or-broader and the instrumentation action must be kept.
    if not standard_condition:
        return True
    return standard_condition == instrumentation_condition


def _action_covered_by_standard(action, resource, condition, standard_statements):
    for standard_statement in standard_statements:
        if action not in standard_statement.get("Action", []):
            continue
        if not _resource_covers(standard_statement.get("Resource", "*"), resource):
            continue
        if not _condition_covers(standard_statement.get("Condition"), condition):
            continue
        return True
    return False


def filter_instrumentation_statements(statements, standard_statements):
    # Only drop an instrumentation action when an existing standard-integration statement
    # grants an equivalent-or-broader permission across Action, Resource, and Condition.
    # Action-name-only filtering would silently drop scoped statements just because the same
    # action name also appears, unconditioned, in the standard policy.
    filtered = []
    for statement in statements:
        resource = statement.get("Resource", "*")
        condition = statement.get("Condition")
        kept_actions = [
            action
            for action in statement.get("Action", [])
            if not _action_covered_by_standard(action, resource, condition, standard_statements)
        ]
        if kept_actions:
            filtered.append({**statement, "Action": kept_actions})
    return filtered


def _policy_document_size(statements):
    return len(json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(',', ':')))


def chunk_statements(statements, max_size=MAX_MANAGED_POLICY_SIZE):
    # Chunk by measured minified-document size, never splitting a statement's Action away from
    # its Resource/Condition. If a single statement alone exceeds max_size it still gets its own
    # (oversize) chunk; validate_statement_chunks is what catches that before anything is deleted.
    chunks = []
    current = []
    for statement in statements:
        candidate = current + [statement]
        if current and _policy_document_size(candidate) > max_size:
            chunks.append(current)
            current = [statement]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def validate_statement_chunks(
    chunks,
    resource_types,
    existing_attached_count=0,
    max_size=MAX_MANAGED_POLICY_SIZE,
    max_policies=MAX_ATTACHED_MANAGED_POLICIES,
):
    # Local size checks are preflight estimates, not a substitute for AWS's own validation on
    # create_policy; this only exists to fail loudly, before touching any existing policy, rather
    # than partially attaching a replacement set that AWS would reject partway through.
    # existing_attached_count is the role's other (non-instrumentation) managed-policy attachments
    # — those aren't being replaced, so they still count against the per-role attachment quota
    # alongside the new instrumentation chunks.
    total = existing_attached_count + len(chunks)
    if total > max_policies:
        raise InstrumentationPolicyLimitError(
            f"Instrumentation permissions for resource types {resource_types} would attach "
            f"{len(chunks)} policies on top of {existing_attached_count} already attached to the "
            f"role, totaling {total} and exceeding the {max_policies}-policy attachment limit."
        )
    for i, chunk in enumerate(chunks):
        size = _policy_document_size(chunk)
        if size > max_size:
            raise InstrumentationPolicyLimitError(
                f"Instrumentation permissions for resource types {resource_types} produced "
                f"policy chunk {i + 1}/{len(chunks)} of {size} characters, exceeding the "
                f"{max_size}-character managed-policy size limit."
            )


def _chunked_policy_names(role_name, prefix, max_policies=MAX_ATTACHED_MANAGED_POLICIES):
    return [f"{prefix}-{role_name}-{i+1}" for i in range(max_policies)]


def _count_non_instrumentation_attached_policies(iam_client, role_name):
    # Exact-name match against what cleanup_instrumentation_policies actually deletes, not a
    # substring check — a stale/unrelated policy whose name merely contains the instrumentation
    # prefix isn't touched by cleanup and must still count against the attachment quota.
    instrumentation_names = set(_chunked_policy_names(role_name, BASE_POLICY_PREFIX_INSTRUMENTATION))
    count = 0
    kwargs = {"RoleName": role_name}
    while True:
        response = iam_client.list_attached_role_policies(**kwargs)
        for policy in response.get("AttachedPolicies", []):
            if policy.get("PolicyName", "") not in instrumentation_names:
                count += 1
        if not response.get("IsTruncated"):
            return count
        kwargs["Marker"] = response["Marker"]


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
    for policy_name in _chunked_policy_names(role_name, prefix, max_policies):
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
    return permissions


def _create_and_attach_policy(iam_client, role_name, policy_name, actions):
    _create_and_attach_statement_policy(
        iam_client, role_name, policy_name, [{"Effect": "Allow", "Action": actions, "Resource": "*"}]
    )


def _create_and_attach_statement_policy(iam_client, role_name, policy_name, statements):
    policy_json = json.dumps(
        {"Version": "2012-10-17", "Statement": statements},
        separators=(',', ':'),
    )
    LOGGER.info(f"Creating policy {policy_name} with {len(statements)} statements ({len(policy_json)} characters)")
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


def attach_instrumentation_permissions(
    iam_client, role_name, account_id, partition, datadog_site, resource_types, previous_resource_types,
    *, standard_statements=(), fail_on_error=False,
):
    # Best-effort by default: instrumentation permissions are additive convenience on top of the
    # integration, so any failure is logged and swallowed rather than blocking install. The
    # post-setup add-on passes fail_on_error=True because attaching these policies is the stack's
    # whole purpose, so a silent SUCCESS that attached nothing would be worse than a visible failure.
    # Fetch before cleanup so that a transient API failure on an Update leaves the
    # previously-attached policies in place instead of silently revoking them.
    #
    # standard_statements defaults to empty (no filtering): filtering is only safe when the caller
    # knows what's actually attached to this role. The role-creation path passes the exact
    # permissions it just attached via attach_standard_permissions in this same invocation; the
    # add-on path (ManageBasePermissions=false) doesn't manage or verify the role's standard
    # permissions, so it can't assume the *current* Datadog standard permission list reflects
    # what's really on the role — filtering against that would risk dropping instrumentation
    # actions the role doesn't actually have covered.
    if not resource_types:
        # Only clean up if the previous Update had instrumentation enabled — avoids running
        # delete calls on stacks that never opted in to instrumentation in the first place.
        if previous_resource_types:
            cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
        return

    try:
        url = build_instrumentation_permissions_url(datadog_site, resource_types)
        LOGGER.info(f"Fetching instrumentation permissions for {resource_types} from {url}")
        attributes = fetch_attributes_from_datadog(url)
        chunks = resolve_instrumentation_statement_chunks(attributes, account_id, partition, standard_statements)
        existing_attached_count = _count_non_instrumentation_attached_policies(iam_client, role_name)
        # Validate the full candidate replacement set before cleanup_instrumentation_policies
        # below deletes anything currently attached — a set that can't fit must fail here,
        # leaving the previously-attached policies untouched.
        validate_statement_chunks(chunks, resource_types, existing_attached_count)
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.warning(
            f"Failed to prepare instrumentation permissions for {resource_types}: {e}. "
            "Leaving any previously-attached instrumentation policies in place."
        )
        return

    cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
    for i, chunk in enumerate(chunks):
        policy_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{i+1}"
        try:
            _create_and_attach_statement_policy(iam_client, role_name, policy_name, chunk)
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
        standard_statements = []
        if manage_base_permissions:
            cleanup_legacy_base_policies(iam_client, role_name, account_id, partition)
            cleanup_existing_policies(iam_client, role_name, account_id, partition)
            standard_permissions = attach_standard_permissions(iam_client, role_name)
            standard_statements = [{"Action": standard_permissions, "Resource": "*"}]
            if should_install_security_audit_policy:
                attach_resource_collection_permissions(iam_client, role_name)
        attach_instrumentation_permissions(
            iam_client, role_name, account_id, partition,
            datadog_site, instrumentation_resource_types, previous_instrumentation_resource_types,
            standard_statements=standard_statements,
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
