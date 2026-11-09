"""One-time PAT bootstrap for AWS Organizations StackSet workload identity.

The mapping is deliberately retained. Ordinary updates and deletion do not use
or renew the PAT. Revoke the mapping in Datadog after deleting stack instances.
"""

import hashlib
import json
import re
import time
import urllib.parse
import uuid

import boto3
import cfnresponse

from wif_auth import DatadogHttpError, WifClient, WifError, WIF_AWS_CONFIG, wif_api_request

_MAPPING_PATH = "/api/v2/cloud_auth/aws/persona_mapping"
_BINDING_PROPERTIES = (
    "DatadogSite", "DatadogOrgUUID", "ServiceAccountId", "WifBrokerRoleArn", "ExpectedAccountId",
)
_REQUIRED_PERMISSIONS = {"aws_configuration_read", "aws_configuration_edit", "aws_configurations_manage"}


def _arn_pattern(role_arn):
    account_id = role_arn.split(":")[4]
    role_name = role_arn.rsplit("/", 1)[1]
    return f"arn:aws:sts::{account_id}:assumed-role/{role_name}/*"


def _binding_digest(properties):
    binding = {key: properties.get(key) for key in _BINDING_PROPERTIES}
    for key in ("DatadogOrgUUID", "ServiceAccountId"):
        if isinstance(binding[key], str):
            binding[key] = binding[key].lower()
    return hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _physical_id(mapping_id, trusted_account, properties):
    # No credential is included. Recording the immutable binding also permits
    # CloudFormation to roll back a rejected update to the original values.
    return f"datadog-wif:v1:{mapping_id}:{trusted_account}:{_binding_digest(properties)}"


def _previous_result(event):
    parts = event.get("PhysicalResourceId", "").split(":")
    if len(parts) != 5 or parts[:2] != ["datadog-wif", "v1"] or not re.fullmatch(r"[0-9]{12}", parts[3]) or not re.fullmatch(r"[0-9a-f]{64}", parts[4]):
        raise WifError("Unknown bootstrap resource identity; do not replace an existing WIF binding in place")
    return {"MappingId": parts[2], "DatadogTrustedAccountId": parts[3]}


def _trusted_account(integration):
    """Reuse the actual integration principal, including on the staging site.

    Only an unambiguous root-account principal gated by this integration's
    exact external ID is accepted. Never guess a staging AWS account number or
    broaden a trust policy containing a specific foreign role.
    """
    auth = integration["attributes"].get("auth_config", {})
    role_name, external_id = auth.get("role_name"), auth.get("external_id")
    if not role_name or not external_id:
        raise WifError("The broker AWS account must have an existing IAM-role-based Datadog integration")
    role = boto3.client("iam", config=WIF_AWS_CONFIG).get_role(RoleName=role_name)["Role"]
    policy = role["AssumeRolePolicyDocument"]
    if isinstance(policy, str):
        policy = json.loads(urllib.parse.unquote(policy))
    account_ids = set()
    statements = policy.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    for statement in statements:
        actions = statement.get("Action", [])
        if isinstance(actions, str):
            actions = [actions]
        if statement.get("Effect") != "Allow" or "sts:AssumeRole" not in actions:
            continue
        required_id = statement.get("Condition", {}).get("StringEquals", {}).get("sts:ExternalId")
        if required_id != external_id and required_id != [external_id]:
            continue
        principals = statement.get("Principal", {}).get("AWS", [])
        if isinstance(principals, str):
            principals = [principals]
        for principal in principals:
            match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):root", principal)
            if not match:
                raise WifError("The existing integration must trust a Datadog AWS account root with an exact external ID")
            account_ids.add(match[1])
    if len(account_ids) != 1:
        raise WifError("Cannot identify one Datadog AWS principal from the broker account's existing integration role")
    return account_ids.pop()


def _create_mapping(properties):
    client = WifClient(properties["DatadogSite"], properties["DatadogOrgUUID"], properties["WifBrokerRoleArn"])
    broker_account = client.role_arn.split(":")[4]
    if properties["ExpectedAccountId"] != broker_account or properties["AccountId"] != broker_account:
        raise WifError("Launch this stack in the already-integrated AWS account selected in Datadog")
    pat = properties.get("BootstrapPAT", "")
    if not pat:
        raise WifError("A fresh bootstrap PAT is required for initial setup")
    auth = {"Authorization": f"Bearer {pat}"}

    def call(path, method="GET", body=None):
        return wif_api_request(client.site, path, auth, method=method, body=body)

    # Validate all prerequisites before creating a persistent mapping.
    accounts = call(f"/api/v2/integration/aws/accounts?aws_account_id={broker_account}").get("data", [])
    accounts = [item for item in accounts if item.get("attributes", {}).get("aws_account_id") == broker_account]
    if len(accounts) != 1:
        raise WifError("The broker AWS account must already be integrated with this Datadog organization")
    trusted_account = _trusted_account(accounts[0])
    try:
        user_id = str(uuid.UUID(properties["ServiceAccountId"]))
    except (ValueError, TypeError, AttributeError):
        raise WifError("ServiceAccountId must be the UUID of an existing Datadog service account") from None
    user = call(f"/api/v2/users/{user_id}")["data"]
    attributes = user["attributes"]
    if not attributes.get("service_account") or attributes.get("disabled"):
        raise WifError("Select an active Datadog service account; human users are not accepted")
    user_org = user.get("relationships", {}).get("org", {}).get("data") or {}
    if user_org.get("id", "").lower() != client.org_uuid:
        raise WifError("The PAT, service account, and DatadogOrgUUID must belong to the same organization")
    permissions = call(f"/api/v2/users/{user_id}/permissions").get("data", [])
    names = {item.get("attributes", {}).get("name") for item in permissions}
    if not _REQUIRED_PERMISSIONS.issubset(names):
        raise WifError("The service account needs AWS Configuration Read, AWS Configuration Edit, and AWS Configurations Manage permissions")
    handle = attributes["handle"]
    pattern = _arn_pattern(client.role_arn)
    mappings = call(_MAPPING_PATH).get("data", [])
    matches = [item for item in mappings if item["attributes"].get("arn_pattern") == pattern]
    if matches:
        if len(matches) != 1 or matches[0]["attributes"].get("account_uuid") != user_id:
            raise WifError("The broker role already has a conflicting WIF mapping; it was not changed")
        mapping = matches[0]
    else:
        try:
            mapping = call(_MAPPING_PATH, method="POST", body={
                "data": {
                    "type": "aws_cloud_auth_config",
                    "attributes": {"arn_pattern": pattern, "account_identifier": handle},
                },
            })["data"]
        except DatadogHttpError as error:
            if error.status == 403:
                raise WifError("Verify WIF is enabled and the bootstrap PAT includes WIF configuration write access and all permissions held by the selected service account") from None
            raise

    # Exercise the same AWS proof and bearer-token API path used by future
    # StackSet instances before marking setup ready. Allow bounded propagation
    # time for the new role trust and org-config mapping, without PAT fallback.
    deadline = time.monotonic() + 90
    for attempt in range(10):
        try:
            wif_api_request(client.site, f"/api/v2/integration/aws/accounts?aws_account_id={broker_account}", client.headers())
            break
        except WifError:
            client.invalidate()
            if attempt == 9 or time.monotonic() >= deadline:
                raise WifError("The mapping was created but WIF verification failed. Check AWS trust, the service account's integration access, and WIF enablement; retry setup or revoke the mapping in Datadog") from None
            time.sleep(min(2 ** attempt, 8))
    return {"MappingId": mapping["id"], "DatadogTrustedAccountId": trusted_account}


def handle(event):
    properties = event["ResourceProperties"]
    request_type = event["RequestType"]
    if request_type == "Delete":
        # Deliberately do not authenticate with the original PAT or remove a
        # mapping still needed by account teardown. Revocation is explicit.
        return {}, event.get("PhysicalResourceId", "datadog-wif-retained")
    if event["ResourceType"] != "Custom::DatadogStackSetWif":
        raise WifError("Unknown bootstrap resource type")
    if request_type == "Update":
        result = _previous_result(event)
        if _binding_digest(properties) != event["PhysicalResourceId"].split(":")[4]:
            raise WifError("WIF identity bindings are immutable in this stack. Create a replacement bootstrap stack and revoke the old mapping explicitly")
        return result, event["PhysicalResourceId"]
    if request_type != "Create":
        raise WifError("Unsupported CloudFormation request")
    result = _create_mapping(properties)
    return result, _physical_id(result["MappingId"], result["DatadogTrustedAccountId"], properties)


def handler(event, context):
    # Never log events: ResourceProperties contains the one-time bootstrap PAT.
    try:
        data, resource_id = handle(event)
        cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physicalResourceId=resource_id)
    except WifError as error:
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physicalResourceId=event.get("PhysicalResourceId"), reason=str(error))
    except Exception:
        # SDK exceptions and response bodies can contain credentials. Keep the
        # customer-facing message bounded and do not dump the request/response.
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physicalResourceId=event.get("PhysicalResourceId"), reason="AWS StackSet WIF bootstrap failed; verify the documented prerequisites and retry with a fresh PAT")
