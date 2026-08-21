import hashlib
import json
import logging
import re
import time
from urllib.request import Request
import urllib.error
import urllib.parse
import urllib.request
import cfnresponse
import boto3

from cfn_common import send_cfn_response

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
INSTRUMENTATION_POLICY_GENERATIONS = ("a", "b")
# Keep this list aligned with every property that affects instrumentation policy
# generation or replacement behavior. Otherwise, an update can retain stale policies.
INSTRUMENTATION_UPDATE_PROPERTIES = (
    "PolicyAttachmentSchemaVersion",
    "DatadogIntegrationRole",
    "AccountId",
    "Partition",
    "InstrumentationResourceTypes",
    "DatadogSite",
    "FailOnInstrumentationError",
)
MAX_POLICY_VERSIONS = 5
MAX_BOUNDARY_POLICY_RECONCILE_ATTEMPTS = 5
BOUNDARY_POLICY_RECONCILE_BASE_DELAY_SECONDS = 1
MAX_BOUNDARY_POLICIES = 10
MAX_MANAGED_POLICY_CHARACTERS = 6144
BOUNDARY_POLICY_PATH = "/datadog/instrumenter-boundaries/"
BOUNDARY_POLICY_MANAGED_TAG_KEY = "DatadogInstrumenterBoundaryManaged"
BOUNDARY_POLICY_MANAGED_TAG_VALUE = "true"
BOUNDARY_POLICY_OWNER_TAG_PREFIX = "DatadogCloudFormationOwner/"
IAM_POLICY_NAME_PATTERN = re.compile(r"[\w+=,.@-]{1,128}", re.ASCII)
FORBIDDEN_BOUNDARY_ACTION_PREFIXES = ("iam:", "sts:", "organizations:")


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


def fetch_instrumentation_permissions(api_url, account_id, partition):
    attributes = fetch_permissions_attributes_from_datadog(api_url)
    policy_documents = attributes.get("policy_documents")
    if not policy_documents:
        raise DatadogAPIError("Datadog API returned no instrumentation policy documents")
    if len(policy_documents) > MAX_POLICY_DOCUMENTS:
        raise DatadogAPIError(
            f"Datadog API returned {len(policy_documents)} instrumentation policy documents; "
            f"at most {MAX_POLICY_DOCUMENTS} are supported"
        )

    boundary_documents = _validate_permissions_boundary_policy_documents(
        attributes.get("permissions_boundary_policy_documents"),
        account_id,
        partition,
    )
    return policy_documents, boundary_documents


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


def _fetch_instrumentation_permissions_for_resource_types(
    datadog_site,
    resource_types,
    account_id,
    partition,
):
    url = build_instrumentation_permissions_url(
        datadog_site,
        resource_types,
        account_id,
        partition,
    )
    LOGGER.info("Fetching instrumentation permissions for %s from %s", resource_types, url)
    return fetch_instrumentation_permissions(url, account_id, partition)


def _boundary_policy_arn(policy_name, account_id, partition):
    return f"arn:{partition}:iam::{account_id}:policy{BOUNDARY_POLICY_PATH}{policy_name}"


def _boundary_policy_owner_tag_key(owner_id):
    owner_hash = hashlib.sha256(owner_id.encode()).hexdigest()
    return f"{BOUNDARY_POLICY_OWNER_TAG_PREFIX}{owner_hash}"


def _boundary_policy_owner_tag(owner_id):
    return {"Key": _boundary_policy_owner_tag_key(owner_id), "Value": owner_id}


def _boundary_policy_tags(owner_id):
    return [
        {
            "Key": BOUNDARY_POLICY_MANAGED_TAG_KEY,
            "Value": BOUNDARY_POLICY_MANAGED_TAG_VALUE,
        },
        _boundary_policy_owner_tag(owner_id),
    ]


def _validate_boundary_role_arn_patterns(patterns, policy_name, account_id, partition):
    if not isinstance(patterns, list) or not patterns:
        raise DatadogAPIError(f"Datadog API returned no role ARN patterns for {policy_name}")

    role_arn_prefix = f"arn:{partition}:iam::{account_id}:role/"
    for pattern in patterns:
        if (
            not isinstance(pattern, str)
            or not pattern.startswith(role_arn_prefix)
            or pattern == role_arn_prefix
        ):
            raise DatadogAPIError(
                f"Datadog API returned role ARN pattern {pattern!r} outside account {account_id}"
            )
        if "?" in pattern or pattern.count("*") > 1 or ("*" in pattern and not pattern.endswith("*")):
            raise DatadogAPIError(
                f"Datadog API returned unsupported role ARN pattern {pattern!r}"
            )


def _validate_boundary_policy_document(policy_document, policy_name):
    if not isinstance(policy_document, dict):
        raise DatadogAPIError(f"Datadog API returned no policy document for {policy_name}")
    if policy_document.get("Version") != "2012-10-17":
        raise DatadogAPIError(
            f"Datadog API returned an invalid policy document version for {policy_name}"
        )

    statements = policy_document.get("Statement")
    if not isinstance(statements, list) or not statements:
        raise DatadogAPIError(f"Datadog API returned no policy statements for {policy_name}")

    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            raise DatadogAPIError(
                f"Datadog API returned an unsupported policy statement for {policy_name}"
            )
        if "NotAction" in statement or "NotResource" in statement:
            raise DatadogAPIError(
                f"Datadog API returned an inverted policy statement for {policy_name}"
            )

        actions = statement.get("Action")
        if isinstance(actions, str):
            actions = [actions]
        if not isinstance(actions, list) or not actions or not all(
            isinstance(action, str) and action for action in actions
        ):
            raise DatadogAPIError(
                f"Datadog API returned invalid policy actions for {policy_name}"
            )
        for action in actions:
            lower_action = action.lower()
            if action == "*" or lower_action.startswith(FORBIDDEN_BOUNDARY_ACTION_PREFIXES):
                raise DatadogAPIError(
                    f"Datadog API returned forbidden boundary action {action!r} for {policy_name}"
                )

        resources = statement.get("Resource")
        if isinstance(resources, str):
            resources = [resources]
        if not isinstance(resources, list) or not resources or not all(
            isinstance(resource, str) and resource for resource in resources
        ):
            raise DatadogAPIError(
                f"Datadog API returned invalid policy resources for {policy_name}"
            )

    policy_json = json.dumps(policy_document, sort_keys=True, separators=(",", ":"))
    if len(policy_json) > MAX_MANAGED_POLICY_CHARACTERS:
        raise DatadogAPIError(
            f"Datadog API returned an oversized policy document for {policy_name}"
        )


def _role_arn_patterns_overlap(left, right):
    left_prefix = left.removesuffix("*")
    right_prefix = right.removesuffix("*")
    if left.endswith("*") and right.endswith("*"):
        return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)
    if left.endswith("*"):
        return right.startswith(left_prefix)
    if right.endswith("*"):
        return left.startswith(right_prefix)
    return left == right


def _validate_permissions_boundary_policy_documents(boundary_documents, account_id, partition):
    if not isinstance(boundary_documents, list) or not boundary_documents:
        raise DatadogAPIError("Datadog API returned no permissions boundary policy documents")
    if len(boundary_documents) > MAX_BOUNDARY_POLICIES:
        raise DatadogAPIError(
            f"Datadog API returned {len(boundary_documents)} permissions boundaries; "
            f"at most {MAX_BOUNDARY_POLICIES} are supported"
        )

    seen_ids = set()
    documents_by_name = {}
    seen_role_patterns = []
    for boundary in boundary_documents:
        if not isinstance(boundary, dict):
            raise DatadogAPIError("Datadog API returned an invalid permissions boundary entry")

        boundary_id = boundary.get("id")
        if not isinstance(boundary_id, str) or not boundary_id:
            raise DatadogAPIError("Datadog API returned a permissions boundary without an ID")
        if boundary_id in seen_ids:
            raise DatadogAPIError(f"Datadog API returned duplicate permissions boundary ID {boundary_id!r}")
        seen_ids.add(boundary_id)

        policy_name = boundary.get("policy_name")
        if not isinstance(policy_name, str) or IAM_POLICY_NAME_PATTERN.fullmatch(policy_name) is None:
            raise DatadogAPIError(f"Datadog API returned invalid permissions boundary name {policy_name!r}")
        policy_name_key = policy_name.casefold()
        if policy_name_key in documents_by_name:
            raise DatadogAPIError(f"Datadog API returned duplicate permissions boundary {policy_name!r}")
        if boundary.get("policy_path") != BOUNDARY_POLICY_PATH:
            raise DatadogAPIError(
                f"Datadog API returned permissions boundary {policy_name!r} outside "
                f"{BOUNDARY_POLICY_PATH}"
            )

        expected_arn = _boundary_policy_arn(policy_name, account_id, partition)
        if boundary.get("policy_arn") != expected_arn:
            raise DatadogAPIError(
                f"Datadog API returned ARN {boundary.get('policy_arn')!r} for {policy_name}; "
                f"expected {expected_arn!r}"
            )
        role_patterns = boundary.get("role_arn_patterns")
        _validate_boundary_role_arn_patterns(role_patterns, policy_name, account_id, partition)
        for pattern in role_patterns:
            for seen_pattern in seen_role_patterns:
                if _role_arn_patterns_overlap(pattern, seen_pattern):
                    raise DatadogAPIError(
                        f"Datadog API returned overlapping role ARN patterns "
                        f"{pattern!r} and {seen_pattern!r}"
                    )
            seen_role_patterns.append(pattern)

        _validate_boundary_policy_document(boundary.get("policy_document"), policy_name)
        documents_by_name[policy_name_key] = boundary

    return [documents_by_name[name] for name in sorted(documents_by_name)]


def _canonical_policy_document(policy_document):
    if isinstance(policy_document, str):
        policy_document = json.loads(urllib.parse.unquote(policy_document))
    if not isinstance(policy_document, dict):
        raise RuntimeError("IAM returned an invalid managed policy document")
    return json.dumps(policy_document, sort_keys=True, separators=(",", ":"))


def _get_policy(iam_client, policy_arn):
    try:
        return iam_client.get_policy(PolicyArn=policy_arn)["Policy"]
    except iam_client.exceptions.NoSuchEntityException:
        return None


def _prune_oldest_policy_version_if_needed(iam_client, policy_arn):
    versions = iam_client.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
    if len(versions) < MAX_POLICY_VERSIONS:
        return

    non_default_versions = [version for version in versions if not version["IsDefaultVersion"]]
    if not non_default_versions:
        raise RuntimeError(f"Policy {policy_arn} reached the version limit with no removable version")
    oldest = min(non_default_versions, key=lambda version: version["CreateDate"])
    iam_client.delete_policy_version(PolicyArn=policy_arn, VersionId=oldest["VersionId"])


def _ensure_permissions_boundary_policy(iam_client, boundary, owner_id):
    policy_name = boundary["policy_name"]
    policy_arn = boundary["policy_arn"]
    policy_json = _canonical_policy_document(boundary["policy_document"])

    for attempt in range(MAX_BOUNDARY_POLICY_RECONCILE_ATTEMPTS):
        policy = _get_policy(iam_client, policy_arn)
        if policy is None:
            try:
                iam_client.create_policy(
                    PolicyName=policy_name,
                    Path=boundary["policy_path"],
                    PolicyDocument=policy_json,
                    Tags=_boundary_policy_tags(owner_id),
                )
                return
            except iam_client.exceptions.EntityAlreadyExistsException:
                iam_client.get_waiter("policy_exists").wait(
                    PolicyArn=policy_arn,
                    WaiterConfig={"Delay": 1, "MaxAttempts": 20},
                )
                continue

        tags = _policy_tags_by_key(iam_client, policy_arn)
        if (
            tags.get(BOUNDARY_POLICY_MANAGED_TAG_KEY)
            == BOUNDARY_POLICY_MANAGED_TAG_VALUE
        ):
            owner_tag_key = _boundary_policy_owner_tag_key(owner_id)
            if owner_tag_key not in tags:
                iam_client.tag_policy(
                    PolicyArn=policy_arn,
                    Tags=[_boundary_policy_owner_tag(owner_id)],
                )
        else:
            LOGGER.warning(
                "Using permissions boundary %s without claiming ownership because it lacks "
                "the Datadog management tag",
                policy_arn,
            )

        default_version_id = policy.get("DefaultVersionId")
        if not default_version_id:
            raise RuntimeError(f"Policy {policy_name} has no default version")
        try:
            current_document = iam_client.get_policy_version(
                PolicyArn=policy_arn,
                VersionId=default_version_id,
            )["PolicyVersion"]["Document"]
            if _canonical_policy_document(current_document) == policy_json:
                return

            _prune_oldest_policy_version_if_needed(iam_client, policy_arn)
            iam_client.create_policy_version(
                PolicyArn=policy_arn,
                PolicyDocument=policy_json,
                SetAsDefault=True,
            )
            return
        except (
            iam_client.exceptions.LimitExceededException,
            iam_client.exceptions.NoSuchEntityException,
        ):
            if attempt == MAX_BOUNDARY_POLICY_RECONCILE_ATTEMPTS - 1:
                raise
            time.sleep(BOUNDARY_POLICY_RECONCILE_BASE_DELAY_SECONDS * 2**attempt)

    raise RuntimeError(f"Policy {policy_name} could not be reconciled")


def manage_permissions_boundaries(iam_client, boundary_documents, owner_id):
    for boundary in boundary_documents:
        _ensure_permissions_boundary_policy(iam_client, boundary, owner_id)


def _list_policy_tags(iam_client, policy_arn):
    tags = []
    request = {"PolicyArn": policy_arn}
    while True:
        response = iam_client.list_policy_tags(**request)
        tags.extend(response.get("Tags", []))
        marker = response.get("Marker") if response.get("IsTruncated") else None
        if not marker:
            return tags
        request["Marker"] = marker


def _policy_tags_by_key(iam_client, policy_arn):
    return {
        tag["Key"]: tag.get("Value", "")
        for tag in _list_policy_tags(iam_client, policy_arn)
        if "Key" in tag
    }


def _list_policy_entities(iam_client, policy_arn):
    entities = {"roles": [], "users": [], "groups": []}
    request = {"PolicyArn": policy_arn}
    while True:
        response = iam_client.list_entities_for_policy(**request)
        entities["roles"].extend(
            role["RoleName"] for role in response.get("PolicyRoles", [])
        )
        entities["users"].extend(
            user["UserName"] for user in response.get("PolicyUsers", [])
        )
        entities["groups"].extend(
            group["GroupName"] for group in response.get("PolicyGroups", [])
        )
        marker = response.get("Marker") if response.get("IsTruncated") else None
        if not marker:
            return entities
        request["Marker"] = marker


def _list_boundary_policies(iam_client):
    policies = []
    request = {"Scope": "Local", "PathPrefix": BOUNDARY_POLICY_PATH}
    while True:
        response = iam_client.list_policies(**request)
        policies.extend(
            {
                "policy_name": policy["PolicyName"],
                "policy_path": policy["Path"],
                "policy_arn": policy["Arn"],
            }
            for policy in response.get("Policies", [])
            if policy.get("Path") == BOUNDARY_POLICY_PATH
        )
        marker = response.get("Marker") if response.get("IsTruncated") else None
        if not marker:
            return policies
        request["Marker"] = marker


def _format_policy_entities(entities):
    return ", ".join(
        f"{entity_type}=[{', '.join(sorted(names))}]"
        for entity_type, names in entities.items()
        if names
    )


def _boundary_owner_tag_keys(tags):
    return {
        key for key in tags if key.startswith(BOUNDARY_POLICY_OWNER_TAG_PREFIX)
    }


def _claim_boundary_ownership(iam_client, policy_arn, owner_id):
    iam_client.tag_policy(
        PolicyArn=policy_arn,
        Tags=[_boundary_policy_owner_tag(owner_id)],
    )


def _delete_permissions_boundary_policy(iam_client, boundary, owner_id):
    policy_arn = boundary["policy_arn"]
    if _get_policy(iam_client, policy_arn) is None:
        return True

    tags = _policy_tags_by_key(iam_client, policy_arn)
    owner_tag_key = _boundary_policy_owner_tag_key(owner_id)
    is_managed = (
        tags.get(BOUNDARY_POLICY_MANAGED_TAG_KEY)
        == BOUNDARY_POLICY_MANAGED_TAG_VALUE
    )
    if not is_managed:
        LOGGER.warning(
            "Preserving permissions boundary %s because it lacks the Datadog management tag",
            policy_arn,
        )
        return False

    owner_tags = _boundary_owner_tag_keys(tags)
    if owner_tag_key not in owner_tags:
        if owner_tags:
            LOGGER.warning(
                "Preserving permissions boundary %s because it is owned by another "
                "CloudFormation stack",
                policy_arn,
            )
            return False
        _claim_boundary_ownership(iam_client, policy_arn, owner_id)

    other_owner_tags = owner_tags - {owner_tag_key}
    if other_owner_tags:
        iam_client.untag_policy(PolicyArn=policy_arn, TagKeys=[owner_tag_key])
        tags = _policy_tags_by_key(iam_client, policy_arn)
        if (
            tags.get(BOUNDARY_POLICY_MANAGED_TAG_KEY)
            != BOUNDARY_POLICY_MANAGED_TAG_VALUE
        ):
            return False
        if _boundary_owner_tag_keys(tags) - {owner_tag_key}:
            LOGGER.warning(
                "Preserving permissions boundary %s because it remains owned by another "
                "CloudFormation stack",
                policy_arn,
            )
            return False
        _claim_boundary_ownership(iam_client, policy_arn, owner_id)

    entities = _list_policy_entities(iam_client, policy_arn)
    if any(entities.values()):
        raise RuntimeError(
            f"Permissions boundary {policy_arn} is still in use by "
            f"{_format_policy_entities(entities)}"
        )

    tags = _policy_tags_by_key(iam_client, policy_arn)
    other_owner_tags = _boundary_owner_tag_keys(tags) - {owner_tag_key}
    if other_owner_tags:
        iam_client.untag_policy(PolicyArn=policy_arn, TagKeys=[owner_tag_key])
        LOGGER.warning(
            "Preserving permissions boundary %s because it became owned by another "
            "CloudFormation stack during cleanup",
            policy_arn,
        )
        return False
    if (
        tags.get(BOUNDARY_POLICY_MANAGED_TAG_KEY)
        != BOUNDARY_POLICY_MANAGED_TAG_VALUE
        or owner_tag_key not in tags
    ):
        return False

    versions = iam_client.list_policy_versions(PolicyArn=policy_arn).get("Versions", [])
    for version in versions:
        if not version["IsDefaultVersion"]:
            iam_client.delete_policy_version(
                PolicyArn=policy_arn,
                VersionId=version["VersionId"],
            )

    try:
        iam_client.delete_policy(PolicyArn=policy_arn)
    except iam_client.exceptions.NoSuchEntityException:
        pass
    except iam_client.exceptions.DeleteConflictException as error:
        entities = _list_policy_entities(iam_client, policy_arn)
        blockers = _format_policy_entities(entities) or "entities IAM did not enumerate"
        raise RuntimeError(
            f"Permissions boundary {policy_arn} is still in use by {blockers}"
        ) from error
    return True


def cleanup_permissions_boundaries(iam_client, owner_id, retained_policy_arns=()):
    retained_policy_arns = set(retained_policy_arns)
    preserved = []
    failures = []
    for boundary in _list_boundary_policies(iam_client):
        if boundary["policy_arn"] in retained_policy_arns:
            continue
        try:
            if not _delete_permissions_boundary_policy(iam_client, boundary, owner_id):
                preserved.append(boundary["policy_arn"])
        except Exception as error:
            failures.append(f"{boundary['policy_arn']}: {error}")
    if failures:
        raise RuntimeError(
            "Failed to clean up permissions boundaries: " + "; ".join(failures)
        )
    return preserved


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
    for generation in (None, *INSTRUMENTATION_POLICY_GENERATIONS):
        _cleanup_instrumentation_policy_generation(
            iam_client,
            role_name,
            account_id,
            partition,
            generation,
            max_policies=max_policies,
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


def _create_policy_document(iam_client, policy_name, policy_document):
    policy_json = json.dumps(policy_document, separators=(',', ':'))
    LOGGER.info(f"Creating policy {policy_name} ({len(policy_json)} characters)")
    policy = iam_client.create_policy(PolicyName=policy_name, PolicyDocument=policy_json)
    return policy['Policy']['Arn']


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


def _instrumentation_policy_name(role_name, index, generation=None):
    suffix = f"{generation or ''}{index}"
    return f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-{suffix}"


def _instrumentation_policy_arn(
    role_name,
    account_id,
    partition,
    index,
    generation=None,
):
    policy_name = _instrumentation_policy_name(role_name, index, generation)
    return f"arn:{partition}:iam::{account_id}:policy/{policy_name}"


def _instrumentation_policy_identity(policy_name, role_name):
    prefix = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{role_name}-"
    if not policy_name.startswith(prefix):
        return None

    suffix = policy_name[len(prefix):]
    generation = None
    if suffix[:1] in INSTRUMENTATION_POLICY_GENERATIONS:
        generation = suffix[0]
        suffix = suffix[1:]
    if not suffix.isdigit():
        return None

    index = int(suffix)
    if not 1 <= index <= MAX_POLICY_DOCUMENTS:
        return None
    return generation, index


def _is_instrumentation_policy(policy_name, role_name):
    return _instrumentation_policy_identity(policy_name, role_name) is not None


def _cleanup_instrumentation_policy_generation(
    iam_client,
    role_name,
    account_id,
    partition,
    generation,
    max_policies=MAX_POLICY_DOCUMENTS,
    fail_on_error=False,
):
    for index in range(1, max_policies + 1):
        policy_name = _instrumentation_policy_name(role_name, index, generation)
        policy_arn = _instrumentation_policy_arn(
            role_name,
            account_id,
            partition,
            index,
            generation,
        )
        _detach_and_delete_policy(
            iam_client,
            role_name,
            policy_arn,
            policy_name,
            fail_on_error=fail_on_error,
        )


def _attached_instrumentation_policies(iam_client, role_name):
    policies = []
    for policy in _list_attached_role_policies(iam_client, role_name):
        identity = _instrumentation_policy_identity(policy["PolicyName"], role_name)
        if identity is None:
            continue
        generation, index = identity
        policies.append(
            {
                "generation": generation,
                "index": index,
                "name": policy["PolicyName"],
                "arn": policy["PolicyArn"],
            }
        )
    return sorted(policies, key=lambda policy: (policy["generation"] or "", policy["index"]))


def _replacement_policy_generation(attached_policies):
    active_generations = {
        policy["generation"]
        for policy in attached_policies
        if policy["generation"] in INSTRUMENTATION_POLICY_GENERATIONS
    }
    if len(active_generations) == len(INSTRUMENTATION_POLICY_GENERATIONS):
        raise RuntimeError("role has instrumentation policies from both replacement generations")
    return "b" if "a" in active_generations else "a"


def _restore_instrumentation_policies(
    iam_client,
    role_name,
    attached_replacements,
    detached_previous,
):
    restored = True
    detached_replacements = []
    for policy in reversed(attached_replacements):
        try:
            iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["arn"])
            detached_replacements.append(policy)
        except Exception as e:
            restored = False
            LOGGER.error(f"Error detaching staged policy {policy['name']}: {str(e)}")

    for policy in detached_previous:
        try:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy["arn"])
        except Exception as e:
            restored = False
            LOGGER.error(f"Error restoring policy {policy['name']}: {str(e)}")
    if restored:
        return True

    for policy in reversed(detached_replacements):
        try:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy["arn"])
        except Exception as e:
            LOGGER.error(f"Error reattaching staged policy {policy['name']}: {str(e)}")
    return restored


def _detach_attached_instrumentation_policy(iam_client, role_name, policy):
    try:
        iam_client.detach_role_policy(RoleName=role_name, PolicyArn=policy["arn"])
        return True
    except iam_client.exceptions.NoSuchEntityException:
        return False


def _replace_instrumentation_policies(
    iam_client,
    role_name,
    account_id,
    partition,
    policy_documents,
):
    previous_policies = _attached_instrumentation_policies(iam_client, role_name)
    replacement_generation = _replacement_policy_generation(previous_policies)

    _cleanup_instrumentation_policy_generation(
        iam_client,
        role_name,
        account_id,
        partition,
        replacement_generation,
        fail_on_error=True,
    )

    replacement_policies = []
    try:
        for index, policy_document in enumerate(policy_documents, start=1):
            policy_name = _instrumentation_policy_name(
                role_name,
                index,
                replacement_generation,
            )
            policy_arn = _create_policy_document(
                iam_client,
                policy_name,
                policy_document,
            )
            replacement_policies.append({"name": policy_name, "arn": policy_arn})
    except Exception:
        _cleanup_instrumentation_policy_generation(
            iam_client,
            role_name,
            account_id,
            partition,
            replacement_generation,
        )
        raise

    detached_previous = []
    attached_replacements = []
    try:
        for policy in previous_policies:
            if _detach_attached_instrumentation_policy(iam_client, role_name, policy):
                detached_previous.append(policy)
        for policy in replacement_policies:
            iam_client.attach_role_policy(RoleName=role_name, PolicyArn=policy["arn"])
            attached_replacements.append(policy)
    except Exception:
        restored = _restore_instrumentation_policies(
            iam_client,
            role_name,
            attached_replacements,
            detached_previous,
        )
        if restored:
            _cleanup_instrumentation_policy_generation(
                iam_client,
                role_name,
                account_id,
                partition,
                replacement_generation,
            )
        raise

    for generation in (None, *INSTRUMENTATION_POLICY_GENERATIONS):
        if generation == replacement_generation:
            continue
        _cleanup_instrumentation_policy_generation(
            iam_client,
            role_name,
            account_id,
            partition,
            generation,
        )


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
    owner_id,
    fail_on_error=False,
):
    # Fetch and validate capacity before staging so a transient failure leaves existing policies.
    replacing_existing_policies = bool(previous_resource_types)
    # Existing policies make updates atomic even when initial attachment was best-effort;
    # accepting new properties while retaining old policies would leave them permanently stale.
    mutation_must_succeed = fail_on_error or replacing_existing_policies
    if not resource_types and not replacing_existing_policies:
        return

    try:
        if resource_types:
            policy_documents, boundary_documents = _fetch_instrumentation_permissions_for_resource_types(
                datadog_site,
                resource_types,
                account_id,
                partition,
            )
            _validate_instrumentation_policy_capacity(
                iam_client,
                role_name,
                len(policy_documents),
            )
            manage_permissions_boundaries(iam_client, boundary_documents, owner_id)
        else:
            policy_documents, boundary_documents = [], []
    except Exception as e:
        if mutation_must_succeed:
            raise
        LOGGER.warning(
            f"Failed to prepare instrumentation permissions for {resource_types}: {e}. "
            "Leaving any previously-attached instrumentation policies in place."
        )
        return

    try:
        if resource_types:
            _replace_instrumentation_policies(
                iam_client,
                role_name,
                account_id,
                partition,
                policy_documents,
            )
        else:
            cleanup_instrumentation_policies(
                iam_client,
                role_name,
                account_id,
                partition,
                fail_on_error=mutation_must_succeed,
            )

        retained_boundary_arns = {
            boundary["policy_arn"] for boundary in boundary_documents
        }
        cleanup_permissions_boundaries(
            iam_client,
            owner_id,
            retained_policy_arns=retained_boundary_arns,
        )
    except Exception as e:
        if mutation_must_succeed:
            raise
        LOGGER.warning(
            f"Failed to reconcile instrumentation permissions for {resource_types}: {e}."
        )


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
        # Fail on in-use boundaries so CloudFormation retains ownership for a cleanup retry.
        preserved_boundaries = cleanup_permissions_boundaries(
            iam_client,
            event["StackId"],
        )
        response_data = {}
        if preserved_boundaries:
            response_data["PreservedPermissionsBoundaries"] = preserved_boundaries
        send_cfn_response(
            cfnresponse, event, context, cfnresponse.SUCCESS, response_data
        )
    except Exception as e:
        LOGGER.error(f"Error deleting policy: {str(e)}")
        send_cfn_response(
            cfnresponse, event, context, cfnresponse.FAILED, {"Message": str(e)}
        )


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
                event["StackId"],
                fail_on_error=fail_on_instrumentation_error,
            )
        if target_changed:
            _cleanup_previous_target_policies(iam_client, previous_props)
        send_cfn_response(cfnresponse, event, context, cfnresponse.SUCCESS, {})
    except Exception as e:
        LOGGER.error(f"Error creating/attaching policy: {str(e)}")
        send_cfn_response(
            cfnresponse, event, context, cfnresponse.FAILED, {"Message": str(e)}
        )


def handler(event, context):
    LOGGER.info("Event received: %s", json.dumps(event))
    if event['RequestType'] == 'Delete':
        handle_delete(event, context)
    else:
        handle_create_update(event, context)
