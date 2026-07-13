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
MANAGED_POLICY_CHAR_LIMIT = 6144


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
    attributes = fetch_permissions_attributes_from_datadog(api_url)
    policy_documents = attributes.get("policy_documents")
    if policy_documents is not None:
        if not policy_documents:
            raise DatadogAPIError("Datadog API returned no instrumentation policy documents")
        for document in policy_documents:
            _serialize_policy_document(document)
        return policy_documents

    permission_chunks = attributes.get("permissions")
    if not permission_chunks:
        raise DatadogAPIError("Datadog API returned neither policy_documents nor permissions")
    if isinstance(permission_chunks[0], str):
        permission_chunks = [permission_chunks]
    LOGGER.warning("Datadog API does not expose policy_documents yet; using legacy broad permissions")
    return [
        {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": chunk, "Resource": "*"}],
        }
        for chunk in permission_chunks
    ]


def parse_resource_types(raw):
    # CFN forwards CommaDelimitedList parameters as JSON arrays to custom resources,
    # while String parameters arrive as comma-delimited strings; accept both.
    if raw is None:
        return []
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    return [t.strip() for t in items if t and t.strip()]


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


def _detach_and_delete_policy(iam_client, role_name, policy_arn, policy_name):
    # Detach + delete are both no-ops if the entity is already gone, so callers can blindly
    # iterate the policy-name space without first checking what actually exists.
    errors = []
    try:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except Exception as e:
        LOGGER.error(f"Error detaching policy {policy_name}: {str(e)}")
        errors.append(f"detach {policy_name}: {e}")

    try:
        iam_client.delete_policy(PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except iam_client.exceptions.DeleteConflictException:
        LOGGER.warning(f"Policy {policy_name} still attached, skipping delete")
        errors.append(f"delete {policy_name}: policy is still attached")
    except Exception as e:
        LOGGER.error(f"Error deleting policy {policy_name}: {str(e)}")
        errors.append(f"delete {policy_name}: {e}")
    return errors


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


def _serialize_policy_document(policy_document):
    if not isinstance(policy_document, dict):
        raise DatadogAPIError("instrumentation policy document must be an object")
    if policy_document.get("Version") != "2012-10-17":
        raise DatadogAPIError("instrumentation policy document has an unsupported version")
    if not isinstance(policy_document.get("Statement"), list) or not policy_document["Statement"]:
        raise DatadogAPIError("instrumentation policy document must contain statements")

    policy_json = json.dumps(policy_document, separators=(',', ':'))
    if len(policy_json) > MANAGED_POLICY_CHAR_LIMIT:
        raise DatadogAPIError(
            f"instrumentation policy document exceeds {MANAGED_POLICY_CHAR_LIMIT} characters"
        )
    return policy_json


def _create_policy(iam_client, policy_name, policy_document):
    policy_json = _serialize_policy_document(policy_document)
    LOGGER.info(f"Creating policy {policy_name} ({len(policy_json)} characters)")
    policy = iam_client.create_policy(PolicyName=policy_name, PolicyDocument=policy_json)
    return policy['Policy']['Arn']


def _create_and_attach_policy(iam_client, role_name, policy_name, actions):
    policy_document = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": actions, "Resource": "*"}],
    }
    policy_arn = _create_policy(iam_client, policy_name, policy_document)
    iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


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


def _list_policy_versions(iam_client, policy_arn):
    versions = []
    marker = None
    while True:
        request = {"PolicyArn": policy_arn}
        if marker:
            request["Marker"] = marker
        response = iam_client.list_policy_versions(**request)
        versions.extend(response.get("Versions", []))
        if not response.get("IsTruncated"):
            return versions
        marker = response["Marker"]


def _policy_quotas(iam_client):
    summary = iam_client.get_account_summary().get("SummaryMap", {})
    attachment_quota = summary.get("AttachedPoliciesPerRoleQuota")
    version_quota = summary.get("VersionsPerPolicyQuota")
    if not isinstance(attachment_quota, int) or attachment_quota < 1:
        raise RuntimeError("IAM account summary did not include AttachedPoliciesPerRoleQuota")
    if not isinstance(version_quota, int) or version_quota < 2:
        raise RuntimeError("IAM account summary did not include VersionsPerPolicyQuota")
    return attachment_quota, version_quota


def _instrumentation_policy_name(role_name, index):
    return f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{index}"


def _instrumentation_policy_index(policy_name, role_name):
    prefix = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-"
    if not policy_name.startswith(prefix):
        return None
    suffix = policy_name[len(prefix):]
    if not suffix.isdigit() or int(suffix) < 1:
        return None
    return int(suffix)


def _stage_policy_version(iam_client, policy_arn, policy_document, version_quota):
    versions = _list_policy_versions(iam_client, policy_arn)
    previous_default = next(
        (version["VersionId"] for version in versions if version.get("IsDefaultVersion")),
        None,
    )
    if previous_default is None:
        raise RuntimeError(f"policy {policy_arn} has no default version")

    non_default_versions = sorted(
        (version for version in versions if not version.get("IsDefaultVersion")),
        key=lambda version: int(version["VersionId"].removeprefix("v")),
    )
    while len(versions) >= version_quota:
        if not non_default_versions:
            raise RuntimeError(f"policy {policy_arn} has no removable non-default version")
        version = non_default_versions.pop(0)
        iam_client.delete_policy_version(
            PolicyArn=policy_arn,
            VersionId=version["VersionId"],
        )
        versions.remove(version)

    response = iam_client.create_policy_version(
        PolicyArn=policy_arn,
        PolicyDocument=_serialize_policy_document(policy_document),
        SetAsDefault=False,
    )
    return {
        "PolicyArn": policy_arn,
        "PreviousVersionId": previous_default,
        "StagedVersionId": response["PolicyVersion"]["VersionId"],
    }


def _discard_staged_replacement(iam_client, role_name, staged_versions, created_policies):
    cleanup_errors = []
    for policy in reversed(created_policies):
        if policy.get("AttachAttempted"):
            try:
                iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
            except iam_client.exceptions.NoSuchEntityException:
                pass
            except Exception as e:
                cleanup_errors.append(f"detach {policy['PolicyArn']}: {e}")
        try:
            iam_client.delete_policy(PolicyArn=policy["PolicyArn"])
        except Exception as e:
            cleanup_errors.append(f"delete {policy['PolicyArn']}: {e}")

    for version in reversed(staged_versions):
        if version.get("Activated"):
            try:
                iam_client.set_default_policy_version(
                    PolicyArn=version["PolicyArn"],
                    VersionId=version["PreviousVersionId"],
                )
            except Exception as e:
                cleanup_errors.append(f"restore {version['PolicyArn']}: {e}")
                continue
        try:
            iam_client.delete_policy_version(
                PolicyArn=version["PolicyArn"],
                VersionId=version["StagedVersionId"],
            )
        except Exception as e:
            cleanup_errors.append(f"delete staged version for {version['PolicyArn']}: {e}")
    return cleanup_errors


def replace_instrumentation_policies(iam_client, role_name, account_id, partition, policy_documents):
    attached_policies = _list_attached_role_policies(iam_client, role_name)
    attachment_quota, version_quota = _policy_quotas(iam_client)
    existing_policies = {}
    for policy in attached_policies:
        index = _instrumentation_policy_index(policy["PolicyName"], role_name)
        if index is not None:
            existing_policies[index] = policy

    non_instrumentation_count = len(attached_policies) - len(existing_policies)
    final_attachment_count = non_instrumentation_count + len(policy_documents)
    if final_attachment_count > attachment_quota:
        raise RuntimeError(
            f"role {role_name} has room for only "
            f"{attachment_quota - non_instrumentation_count} instrumentation policies, "
            f"but Datadog returned {len(policy_documents)}"
        )

    LOGGER.info(
        f"Replacing {len(existing_policies)} instrumentation policies with "
        f"{len(policy_documents)} documents; final managed-policy usage "
        f"{final_attachment_count}/{attachment_quota}"
    )
    ordered_existing_policies = [
        policy for _, policy in sorted(existing_policies.items())
    ]
    used_policy_indices = set(existing_policies)
    next_policy_index = 1
    reused_policy_arns = set()
    staged_versions = []
    created_policies = []
    try:
        for document_index, policy_document in enumerate(policy_documents):
            existing = (
                ordered_existing_policies[document_index]
                if document_index < len(ordered_existing_policies)
                else None
            )
            if existing:
                reused_policy_arns.add(existing["PolicyArn"])
                staged_versions.append(
                    _stage_policy_version(
                        iam_client,
                        existing["PolicyArn"],
                        policy_document,
                        version_quota,
                    )
                )
                continue

            while next_policy_index in used_policy_indices:
                next_policy_index += 1
            policy_name = _instrumentation_policy_name(role_name, next_policy_index)
            used_policy_indices.add(next_policy_index)
            policy_arn = f"arn:{partition}:iam::{account_id}:policy/{policy_name}"
            try:
                iam_client.delete_policy(PolicyArn=policy_arn)
            except iam_client.exceptions.NoSuchEntityException:
                pass
            created_policies.append({
                "PolicyArn": _create_policy(iam_client, policy_name, policy_document),
                "AttachAttempted": False,
            })

        for version in staged_versions:
            iam_client.set_default_policy_version(
                PolicyArn=version["PolicyArn"],
                VersionId=version["StagedVersionId"],
            )
            version["Activated"] = True
        for policy in created_policies:
            policy["AttachAttempted"] = True
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
    except Exception as e:
        cleanup_errors = _discard_staged_replacement(
            iam_client,
            role_name,
            staged_versions,
            created_policies,
        )
        if cleanup_errors:
            raise RuntimeError(
                f"failed to replace instrumentation policies: {e}; "
                f"rollback errors: {'; '.join(cleanup_errors)}"
            ) from e
        raise

    cleanup_errors = []
    for policy in existing_policies.values():
        if policy["PolicyArn"] in reused_policy_arns:
            continue
        cleanup_errors.extend(_detach_and_delete_policy(
            iam_client,
            role_name,
            policy["PolicyArn"],
            policy["PolicyName"],
        ))
    if cleanup_errors:
        raise RuntimeError(
            f"new instrumentation policies are active, but old policy cleanup failed: "
            f"{'; '.join(cleanup_errors)}"
        )


def attach_instrumentation_permissions(iam_client, role_name, account_id, partition, datadog_site, resource_types, previous_resource_types, fail_on_error=False):
    # Best-effort by default: instrumentation permissions are additive convenience on top of the
    # integration, so any failure is logged and swallowed rather than blocking install. The
    # post-setup add-on passes fail_on_error=True because attaching these policies is the stack's
    # whole purpose, so a silent SUCCESS that attached nothing would be worse than a visible failure.
    # Fetch and stage replacements before activation so failures can restore the
    # previously-attached policy versions.
    if not resource_types:
        # Only clean up if the previous Update had instrumentation enabled — avoids running
        # delete calls on stacks that never opted in to instrumentation in the first place.
        if previous_resource_types:
            cleanup_instrumentation_policies(iam_client, role_name, account_id, partition)
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
        replace_instrumentation_policies(
            iam_client,
            role_name,
            account_id,
            partition,
            policy_documents,
        )
    except Exception as e:
        if fail_on_error:
            raise
        LOGGER.warning(
            f"Failed to update instrumentation permissions for {resource_types}: {e}. "
            "Leaving any previously-attached instrumentation policies in place."
        )
        return


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
