import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote, urlsplit

import boto3
import cfnresponse
from botocore.config import Config
from botocore.exceptions import ClientError

from cfn_common import send_cfn_response

STACK_NAME = "DatadogAgentInstallation"
FORWARDING_ROLE = "datadog-eventbridge-cross-region-role"
CONNECTION = "datadog-agent-resource-update-intake-connection"
DESTINATION = "datadog-agent-resource-update-intake-destination"
RULE_PREFIX = "datadog-agent-resource-update-rule-"
FORWARDING_RESOURCE = "DatadogAgentResourceUpdateForwardingStack"
SUPPLEMENTARY_RESOURCE = "DatadogAgentSupplementaryForwardingStack"
SUPPLEMENTARY_RULE_PREFIX = "datadog-agent-supp-"
SUPPLEMENTARY_OWNER_TAG = "datadog:agent-installation-owner"
PERMISSIONS_RESOURCE = "DatadogIntegrationPermissionsStack"
OWNED_PERMISSIONS_RESOURCE = "DatadogAgentInstallationPermissionsStack"
LEGACY_VERSION = "v4.21.1"
# Fixture YAML/JSON hashes after normalizing the approved URLs and release header.
LEGACY_TEMPLATE_HASHES = {
    "070b77c6418fe36d0986ce9efba4c52eaa9b72a4373ff4046f0b5cacd1f09d2e",
    "8f0afb417d9aa5c3146b488966d6f1447deea6a07816cc66083c9db338be2b42",
}
INSTALLATION_RESOURCES = {
    PERMISSIONS_RESOURCE,
    OWNED_PERMISSIONS_RESOURCE,
    FORWARDING_RESOURCE,
    SUPPLEMENTARY_RESOURCE,
}
SUPPORTED_TYPES = {
    "aws:ec2:instance",
    "aws:ecs:cluster",
    "aws:eks:cluster",
    "aws:lambda:function",
}
FORWARDED_TYPES = SUPPORTED_TYPES - {"aws:ecs:cluster"}
NO_API_DESTINATIONS = {
    "ap-east-2",
    "ap-southeast-5",
    "ca-west-1",
    "il-central-1",
    "mx-central-1",
    "us-gov-east-1",
    "us-gov-west-1",
}
# Non-commercial endpoints remain available for launcher deletion and failed-create cleanup.
HOME_REGIONS = {
    "aws": "us-east-1",
    "aws-us-gov": "us-gov-west-1",
    "aws-cn": "cn-north-1",
}
STABLE_STATES = {"CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"}
FAILED_STATES = {
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
    "DELETE_COMPLETE",
    "DELETE_FAILED",
}
SDK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={"mode": "standard", "total_max_attempts": 3},
)


class InstallationError(Exception):
    def __init__(self, message, stack_id=None):
        super().__init__(message)
        self.stack_id = stack_id


def resource_types(value):
    values = {part.strip() for part in value.split(",") if part.strip()}
    if not values <= SUPPORTED_TYPES:
        raise InstallationError(
            "Unsupported instrumentation resource type. Update the launcher template."
        )
    return values


def request_deadline(event):
    deadline = time.time() + 3300
    query = parse_qs(urlsplit(event["ResponseURL"]).query)
    if "X-Amz-Date" in query and "X-Amz-Expires" in query:
        signed_at = (
            datetime.strptime(query["X-Amz-Date"][0], "%Y%m%dT%H%M%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
        deadline = min(deadline, signed_at + int(query["X-Amz-Expires"][0]) - 60)
    return deadline


def parameters(stack):
    return {
        entry["ParameterKey"]: entry.get("ParameterValue", "")
        for entry in stack.get("Parameters", [])
    }


@dataclass(frozen=True)
class ForwardingPlan:
    create_pipeline: bool = False
    supplementary_types: frozenset[str] = frozenset()
    destination_arn: str = ""


def supplementary_rule_name(stack_id, resource_type):
    return (
        SUPPLEMENTARY_RULE_PREFIX
        + stack_id.rsplit("/", 1)[1]
        + "-"
        + resource_type.split(":")[1]
    )


def change_set_parameters(props, types, forwarding, refresh_token, previous, legacy):
    values = {
        key: props[key]
        for key in ("AccountId", "IAMRoleName", "DatadogSite", "ExternalId")
    }
    values.update(
        InstrumentationResourceTypes=",".join(sorted(types)),
        CreateResourceUpdateForwarding=str(forwarding.create_pipeline).lower(),
        SupplementaryResourceTypes=",".join(sorted(forwarding.supplementary_types)),
        ExistingApiDestinationArn=forwarding.destination_arn,
        InstrumentationRefreshToken=refresh_token,
    )
    values.update(legacy)
    params = [
        {"ParameterKey": key, "ParameterValue": value} for key, value in values.items()
    ]
    if previous is not None:
        # v4.22.0 removed APPKey from the installation template.
        params.extend(
            {"ParameterKey": key, "UsePreviousValue": True}
            for key in previous
            if key not in values and key != "APPKey"
        )
    else:
        params.append({"ParameterKey": "APIKey", "ParameterValue": props["APIKey"]})
    return params


def is_known_legacy_template(body):
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = body.replace("\r\n", "\n").strip()
    if isinstance(body, dict):
        body = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if not isinstance(body, str):
        return False
    prefix = os.environ["INSTALLATION_TEMPLATE_URL"].rsplit("/aws/", 1)[0]
    body = body.replace(
        f"{prefix}/aws/{LEGACY_VERSION}/",
        "https://legacy-template.invalid/aws/release/",
    ).replace(f"# version: {LEGACY_VERSION}", "# version: legacy")
    return hashlib.sha256(body.encode()).hexdigest() in LEGACY_TEMPLATE_HASHES


def fingerprint(stack):
    return str(stack.get("LastUpdatedTime") or stack["CreationTime"])


def request_name(event):
    identity = event["StackId"] + event["RequestId"]
    return "datadog-agent-" + hashlib.sha256(identity.encode()).hexdigest()[:40]


def stack_url(stack_id):
    partition, region = stack_id.split(":")[1], stack_id.split(":")[3]
    host = {
        "aws": "console.aws.amazon.com",
        "aws-us-gov": "console.amazonaws-us-gov.com",
        "aws-cn": "console.amazonaws.cn",
    }[partition]
    return f"https://{host}/cloudformation/home?region={region}#/stacks/stackinfo?stackId={quote(stack_id, safe='')}"


def version_tuple(value):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", value):
        raise InstallationError(
            "The installation template version is not recognized. Launch the latest template from Datadog."
        )
    return tuple(int(part) for part in value[1:].split("."))


def expected_pattern(resource_type):
    service = resource_type.split(":")[1]
    events, tag_events, tag_filter = {
        "ec2": (
            ["RunInstances", "ModifyInstanceAttribute"],
            ["CreateTags", "DeleteTags"],
            {"resourcesSet": {"items": {"resourceId": [{"prefix": "i-"}]}}},
        ),
        "eks": (
            ["CreateCluster", "UpdateClusterConfig", "UpdateClusterVersion"],
            ["TagResource", "UntagResource"],
            {"resourceArn": [{"wildcard": "*:cluster/*"}]},
        ),
        "lambda": (
            ["CreateFunction20150331", "UpdateFunctionConfiguration20150331v2"],
            ["TagResource20170331v2", "UntagResource20170331v2"],
            {"resource": [{"wildcard": "*:function:*"}]},
        ),
    }[service]
    return {
        "source": [f"aws.{service}"],
        "detail-type": ["AWS API Call via CloudTrail"],
        "detail": {
            "errorCode": [{"exists": False}],
            "$or": [
                {"eventName": events},
                {"eventName": tag_events, "requestParameters": tag_filter},
            ],
        },
    }


class Launcher:
    def __init__(self, session, partition):
        self.session = session
        self.partition = partition
        self.home = HOME_REGIONS[partition]
        self.iam = session.client("iam", region_name=self.home, config=SDK_CONFIG)
        self.ec2 = session.client("ec2", region_name=self.home, config=SDK_CONFIG)
        self.regions = sorted(
            entry["RegionName"]
            for entry in self.ec2.describe_regions(AllRegions=False)["Regions"]
        )
        if not self.regions:
            raise InstallationError(
                "AWS returned no enabled regions. No changes were made."
            )
        self.cloudformation = {
            region: session.client(
                "cloudformation", region_name=region, config=SDK_CONFIG
            )
            for region in self.regions
        }
        self.events = {
            region: session.client("events", region_name=region, config=SDK_CONFIG)
            for region in self.regions
            if region not in NO_API_DESTINATIONS
        }

    def find_stack(self):
        def describe(region):
            try:
                return self.cloudformation[region].describe_stacks(
                    StackName=STACK_NAME
                )["Stacks"][0]
            except ClientError as error:
                # CloudFormation has no modeled exception for a missing stack.
                if (
                    error.response["Error"]["Code"] == "ValidationError"
                    and error.response["Error"].get("Message")
                    == f"Stack with id {STACK_NAME} does not exist"
                ):
                    return None
                raise

        with ThreadPoolExecutor(max_workers=4) as executor:
            stacks = [
                stack
                for stack in executor.map(describe, self.regions)
                if stack is not None
            ]
        if len(stacks) > 1:
            raise InstallationError(
                "Multiple DatadogAgentInstallation stacks exist in this account. Contact support before updating them."
            )
        return stacks[0] if stacks else None

    def forwarding_role(self):
        try:
            return self.iam.get_role(RoleName=FORWARDING_ROLE)["Role"]
        except self.iam.exceptions.NoSuchEntityException:
            return None

    def pipeline(self, region):
        client = self.events[region]
        try:
            connection = client.describe_connection(Name=CONNECTION)
        except client.exceptions.ResourceNotFoundException:
            connection = None
        try:
            destination = client.describe_api_destination(Name=DESTINATION)
        except client.exceptions.ResourceNotFoundException:
            destination = None
        return connection, destination

    def assert_no_forwarding(self):
        def has_resources(region):
            connection, destination = self.pipeline(region)
            if connection or destination:
                return True
            for prefix in (RULE_PREFIX, SUPPLEMENTARY_RULE_PREFIX):
                rules = self.events[region].list_rules(NamePrefix=prefix, Limit=1)
                if rules.get("Rules") or rules.get("NextToken"):
                    return True
            return False

        with ThreadPoolExecutor(max_workers=4) as executor:
            found = list(executor.map(has_resources, self.events))
        if any(found):
            raise InstallationError(
                "Forwarding resources exist without their forwarding role. Repair the existing setup before retrying."
            )

    def assert_external_owner(self, owner_id, expected_identity=None):
        if not owner_id:
            if expected_identity:
                raise InstallationError(
                    "Existing forwarding has no verifiable integration owner. Reconcile its ownership before retrying; no changes were made."
                )
            return
        client = self.cloudformation[owner_id.split(":")[3]]
        owner = client.describe_stacks(StackName=owner_id)["Stacks"][0]
        root_id = owner.get("RootId", owner["StackId"])
        resources = client.describe_stack_resources(StackName=root_id)["StackResources"]
        if any(
            resource["LogicalResourceId"] == PERMISSIONS_RESOURCE
            for resource in resources
        ):
            raise InstallationError(
                "Another installation stack already owns forwarding. Update that stack or contact support instead of creating a second permissions owner.",
                root_id,
            )
        if expected_identity:
            root = (
                owner
                if root_id == owner["StackId"]
                else client.describe_stacks(StackName=root_id)["Stacks"][0]
            )
            identity = parameters(root)
            if not identity.get("ExternalId"):
                role_stack_id = next(
                    (
                        resource.get("PhysicalResourceId")
                        for resource in resources
                        if resource["LogicalResourceId"]
                        == "DatadogIntegrationRoleStack"
                        and resource.get("ResourceType") == "AWS::CloudFormation::Stack"
                        and resource.get("ResourceStatus") != "DELETE_COMPLETE"
                    ),
                    None,
                )
                if role_stack_id:
                    role_stack = client.describe_stacks(StackName=role_stack_id)[
                        "Stacks"
                    ][0]
                    nested_identity = parameters(role_stack)
                    if role_stack.get("ParentId") != root_id or any(
                        nested_identity.get(key) != identity.get(key)
                        for key in ("IAMRoleName", "DatadogSite")
                    ):
                        raise InstallationError(
                            "The QuickStart role stack does not match its integration owner. No changes were made.",
                            root_id,
                        )
                    identity["ExternalId"] = nested_identity.get("ExternalId", "")
            if any(
                not identity.get(key) or identity[key] != expected_identity[key]
                for key in ("IAMRoleName", "DatadogSite", "ExternalId")
            ):
                raise InstallationError(
                    "Existing forwarding belongs to a different or unverifiable Datadog integration. Reconcile its owning stack before retrying; no changes were made.",
                    root_id,
                )

    def assert_integration_role_identity(self, props):
        role = self.iam.get_role(RoleName=props["IAMRoleName"])["Role"]
        owner_id = next(
            (
                tag["Value"]
                for tag in role.get("Tags", [])
                if tag["Key"] == "aws:cloudformation:stack-id"
            ),
            None,
        )
        if not owner_id:
            raise InstallationError(
                "The legacy installation's integration role has no verifiable QuickStart owner. No changes were made."
            )
        client = self.cloudformation[owner_id.split(":")[3]]
        owner = client.describe_stacks(StackName=owner_id)["Stacks"][0]
        identity = parameters(owner)
        resources = client.describe_stack_resources(StackName=owner_id)[
            "StackResources"
        ]
        if any(
            identity.get(key) != props[key]
            for key in ("IAMRoleName", "DatadogSite", "ExternalId")
        ) or not any(
            resource.get("ResourceType") == "AWS::IAM::Role"
            and resource.get("PhysicalResourceId") == props["IAMRoleName"]
            and resource.get("ResourceStatus") != "DELETE_COMPLETE"
            for resource in resources
        ):
            raise InstallationError(
                "The legacy installation's integration identity could not be verified. No changes were made."
            )
        statements = role.get("AssumeRolePolicyDocument", {}).get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        principal = (
            f"arn:{self.partition}:iam::{identity.get('DdAWSAccountId', '')}:root"
        )
        if len(statements) != 1:
            raise InstallationError(
                "The legacy integration role has ambiguous trust. No changes were made."
            )
        trust = statements[0]
        if (
            not re.fullmatch(r"[0-9]{12}", identity.get("DdAWSAccountId", ""))
            or trust.get("Effect") != "Allow"
            or trust.get("Principal", {}).get("AWS") not in (principal, [principal])
            or trust.get("Action") not in ("sts:AssumeRole", ["sts:AssumeRole"])
            or trust.get("Condition", {}).get("StringEquals", {}).get("sts:ExternalId")
            != props["ExternalId"]
        ):
            raise InstallationError(
                "The legacy integration role's trust does not match its QuickStart identity. No changes were made."
            )
        self.assert_external_owner(owner_id, props)

    def legacy_permissions_configuration(
        self, stack, permissions_stack, resources, props, region
    ):
        previous = parameters(stack)
        version = previous.get("LegacyPermissionsTemplateVersion", "")
        owned_id = next(
            (
                resource["PhysicalResourceId"]
                for resource in resources
                if resource["LogicalResourceId"] == OWNED_PERMISSIONS_RESOURCE
            ),
            None,
        )
        child = parameters(permissions_stack)
        if version:
            if (
                version != LEGACY_VERSION
                or not owned_id
                or not previous.get("ExternalId")
            ):
                raise InstallationError(
                    "The adopted installation's permissions ownership is inconsistent. No changes were made."
                )
            owned = self.cloudformation[region].describe_stacks(StackName=owned_id)[
                "Stacks"
            ][0]
            if parameters(owned).get("InstrumentationPolicyScope") != "stack":
                raise InstallationError(
                    "The installation's managed permissions are not stack-owned. No changes were made."
                )
            frozen_types = previous.get("LegacyInstrumentationResourceTypes")
        else:
            template = self.cloudformation[region].get_template(
                StackName=stack["StackId"], TemplateStage="Original"
            )["TemplateBody"]
            if owned_id or not is_known_legacy_template(template):
                raise InstallationError(
                    "This legacy installation template is not supported for automatic adoption. Contact support; no changes were made.",
                    stack["StackId"],
                )
            self.assert_integration_role_identity(props)
            frozen_types = child.get("InstrumentationResourceTypes")
            if frozen_types is None or resource_types(frozen_types) != resource_types(
                previous.get("InstrumentationResourceTypes", "")
            ):
                raise InstallationError(
                    "The legacy permissions do not match the installation's configured types. No changes were made."
                )
        expected = {
            "IAMRoleName": props["IAMRoleName"],
            "DatadogSite": props["DatadogSite"],
            "InstrumentationResourceTypes": frozen_types,
            "ResourceCollectionPermissions": "false",
            "ManageBasePermissions": "false",
            "FailOnInstrumentationError": "true",
            "AgentlessScanning": "false",
        }
        if (
            frozen_types is None
            or child != expected
            or permissions_stack.get("ParentId") != stack["StackId"]
        ):
            raise InstallationError(
                "The legacy permissions child has an unsupported configuration. No changes were made."
            )
        resource_types(frozen_types)
        return {
            "LegacyPermissionsTemplateVersion": LEGACY_VERSION,
            "LegacyInstrumentationResourceTypes": frozen_types,
        }

    def installation_configuration(self, stack, region, props):
        if not stack:
            return {}, {}
        resources = self.cloudformation[region].describe_stack_resources(
            StackName=stack["StackId"]
        )["StackResources"]
        resources = [
            resource
            for resource in resources
            if resource.get("ResourceStatus") != "DELETE_COMPLETE"
        ]
        if not any(
            resource["LogicalResourceId"] == PERMISSIONS_RESOURCE
            for resource in resources
        ) or any(
            resource["LogicalResourceId"] not in INSTALLATION_RESOURCES
            or resource.get("ResourceType") != "AWS::CloudFormation::Stack"
            for resource in resources
        ):
            raise InstallationError(
                "The existing stack has an unrecognized or customized resource layout. Review it manually in CloudFormation; no changes were made."
            )
        permissions_id = next(
            resource["PhysicalResourceId"]
            for resource in resources
            if resource["LogicalResourceId"] == PERMISSIONS_RESOURCE
        )
        permissions_stack = self.cloudformation[region].describe_stacks(
            StackName=permissions_id
        )["Stacks"][0]
        previous = parameters(stack)
        policy_scope = parameters(permissions_stack).get(
            "InstrumentationPolicyScope", "role"
        )
        legacy = {}
        if policy_scope == "role":
            legacy = self.legacy_permissions_configuration(
                stack, permissions_stack, resources, props, region
            )
        elif (
            policy_scope != "stack"
            or previous.get("LegacyPermissionsTemplateVersion")
            or any(
                resource["LogicalResourceId"] == OWNED_PERMISSIONS_RESOURCE
                for resource in resources
            )
        ):
            raise InstallationError(
                "The installation's permissions ownership is inconsistent. No changes were made."
            )
        if not previous.get("ExternalId") and not legacy:
            raise InstallationError(
                "This installation predates verified integration ownership. Reconcile its integration identity before using the launcher; no changes were made.",
                stack["StackId"],
            )
        forwarding = {
            resource["LogicalResourceId"]: resource["PhysicalResourceId"]
            for resource in resources
            if resource["LogicalResourceId"]
            not in {PERMISSIONS_RESOURCE, OWNED_PERMISSIONS_RESOURCE}
        }
        if len(forwarding) > 1 or (
            previous.get("SupplementaryResourceTypes")
            and SUPPLEMENTARY_RESOURCE not in forwarding
        ):
            raise InstallationError(
                "The installation's forwarding ownership is inconsistent. Review its nested stacks before retrying."
            )
        return forwarding, legacy

    def rule_covers(
        self, region, name, resource_type, destination_arn, role_arn=None, owner_id=None
    ):
        client = self.events[region]
        try:
            rule = client.describe_rule(Name=name)
        except client.exceptions.ResourceNotFoundException:
            return False
        if owner_id:
            tags = {
                tag["Key"]: tag["Value"]
                for tag in client.list_tags_for_resource(ResourceARN=rule["Arn"])[
                    "Tags"
                ]
            }
            if tags.get(SUPPLEMENTARY_OWNER_TAG) != owner_id:
                raise InstallationError(
                    "A supplementary rule does not belong to this installation. No resources were adopted."
                )
        targets = client.list_targets_by_rule(Rule=name)
        try:
            pattern = json.loads(rule.get("EventPattern", ""))
        except ValueError:
            pattern = None
        if (
            rule.get("State") != "ENABLED"
            or pattern != expected_pattern(resource_type)
            or targets.get("NextToken")
            or not any(
                target.get("Arn") == destination_arn
                and target.get("RoleArn")
                and (role_arn is None or target["RoleArn"] == role_arn)
                for target in targets.get("Targets", [])
            )
        ):
            raise InstallationError(
                f"Existing forwarding is incompatible with {resource_type}. Review its owning stack before retrying."
            )
        return True

    def supplementary_configuration(
        self, stack_id, region, types, destination_arn, account
    ):
        if not stack_id:
            return set(), None
        client = self.cloudformation[region]
        child = client.describe_stacks(StackName=stack_id)["Stacks"][0]
        previous = parameters(child)
        owned_types = resource_types(previous.get("InstrumentationResourceTypes", ""))
        if (
            not owned_types
            or not owned_types <= types & FORWARDED_TYPES
            or previous.get("ExistingApiDestinationArn") != destination_arn
        ):
            raise InstallationError(
                "The supplementary forwarding configuration does not match the installation. Review its nested stack before retrying."
            )
        resources = client.describe_stack_resources(StackName=stack_id)[
            "StackResources"
        ]
        role_name = next(
            (
                resource["PhysicalResourceId"]
                for resource in resources
                if resource["LogicalResourceId"] == "DDEventBridgeInvocationRole"
                and resource["ResourceType"] == "AWS::IAM::Role"
                and resource.get("ResourceStatus") != "DELETE_COMPLETE"
            ),
            None,
        )
        if not role_name:
            raise InstallationError(
                "The supplementary forwarding invocation role is missing. Review its nested stack before retrying."
            )
        invocation_role = f"arn:{self.partition}:iam::{account}:role/{role_name}"
        return owned_types, invocation_role

    def supplementary_types(self, stack_id, region, types, destination_arn, account):
        owned_types, invocation_role = self.supplementary_configuration(
            stack_id, region, types, destination_arn, account
        )
        expected_names = {
            supplementary_rule_name(stack_id, resource_type)
            for resource_type in owned_types
        }
        names = {
            rule["Name"]
            for page in self.events[region]
            .get_paginator("list_rules")
            .paginate(NamePrefix=SUPPLEMENTARY_RULE_PREFIX)
            for rule in page["Rules"]
        }
        if names != expected_names:
            raise InstallationError(
                "Supplementary forwarding rules have missing or unrecognized ownership. Review their owning stacks before retrying; no resources were adopted."
            )
        for resource_type in sorted(owned_types):
            if not self.rule_covers(
                region,
                supplementary_rule_name(stack_id, resource_type),
                resource_type,
                destination_arn,
                role_arn=invocation_role,
                owner_id=stack_id,
            ):
                raise InstallationError(
                    "An installation-owned supplementary forwarding rule is missing. Review its nested stack before retrying."
                )
        return owned_types

    def forwarding_plan(self, owned_stacks, role, region, types, props):
        owned_forwarding_stack_id = owned_stacks.get(FORWARDING_RESOURCE)
        supplementary_stack_id = owned_stacks.get(SUPPLEMENTARY_RESOURCE)
        tags = {tag["Key"]: tag["Value"] for tag in (role or {}).get("Tags", [])}
        if not types & FORWARDED_TYPES:
            if owned_stacks:
                raise InstallationError(
                    "This update would remove installation-owned forwarding. Review the installation stack manually in CloudFormation; no changes were made."
                )
            self.assert_external_owner(tags.get("aws:cloudformation:stack-id"))
            return ForwardingPlan()
        if not role:
            if owned_stacks:
                raise InstallationError(
                    "The forwarding role is missing for this installation. Review its owning stack before retrying; no pipeline was recreated."
                )
            self.assert_no_forwarding()
            return ForwardingPlan(create_pipeline=region in self.events)

        primary = tags.get("datadog:primary-region")
        if primary not in self.events:
            raise InstallationError(
                "The forwarding role has no supported, enabled primary region. Repair the existing setup before retrying."
            )
        if owned_forwarding_stack_id and (
            tags.get("aws:cloudformation:stack-id") != owned_forwarding_stack_id
            or primary != region
        ):
            raise InstallationError(
                "Forwarding ownership does not match the existing installation stack. No changes were made."
            )
        connection, destination = self.pipeline(primary)
        if (
            not connection
            or not destination
            or connection.get("ConnectionState") != "AUTHORIZED"
            or destination.get("ApiDestinationState") != "ACTIVE"
            or destination.get("ConnectionArn") != connection.get("ConnectionArn")
            or destination.get("InvocationEndpoint")
            != f"https://api.{props['DatadogSite']}/api/unstable/instrumenter/events"
            or destination.get("HttpMethod") != "POST"
        ):
            raise InstallationError(
                "Existing forwarding is incomplete or points to a different Datadog site. Repair it before retrying."
            )
        if owned_forwarding_stack_id:
            # Keep this stack's resource in its template; discovery must not turn off its condition on updates.
            return ForwardingPlan(create_pipeline=True)
        self.assert_external_owner(tags.get("aws:cloudformation:stack-id"), props)
        if supplementary_stack_id and primary != region:
            raise InstallationError(
                "Supplementary forwarding no longer matches the pipeline's primary region. Review the installation's region before retrying."
            )
        destination_arn = destination["ApiDestinationArn"]
        supplementary = self.supplementary_types(
            supplementary_stack_id, primary, types, destination_arn, props["AccountId"]
        )
        for resource_type in sorted((types & FORWARDED_TYPES) - supplementary):
            if not self.rule_covers(
                primary,
                RULE_PREFIX + resource_type.split(":")[1],
                resource_type,
                destination_arn,
            ):
                supplementary.add(resource_type)
        if not supplementary:
            return ForwardingPlan()
        if primary != region:
            raise InstallationError(
                "Supplementary forwarding must be in the existing pipeline's primary region. Review the installation's region before adding resource types."
            )
        if not re.fullmatch(
            rf"arn:{self.partition}:events:{primary}:{props['AccountId']}:api-destination/{DESTINATION}/[A-Za-z0-9-]+",
            destination_arn,
        ):
            raise InstallationError(
                "The existing API destination ARN does not match this account and forwarding region. No changes were made."
            )
        return ForwardingPlan(
            supplementary_types=frozenset(supplementary),
            destination_arn=destination_arn,
        )

    def prepare(self, event):
        props = event["ResourceProperties"]
        types = resource_types(props["InstrumentationResourceTypes"])
        if not props.get("ExternalId"):
            raise InstallationError(
                "The Datadog integration external ID is missing. Open a fresh launcher from the selected integration."
            )
        if not types:
            raise InstallationError(
                "Select at least one resource type before launching."
            )
        stack = self.find_stack()
        name = request_name(event)
        if stack:
            region = stack["StackId"].split(":")[3]
            client = self.cloudformation[region]
            previous = parameters(stack)
            try:
                change_set = client.describe_change_set(
                    StackName=stack["StackId"], ChangeSetName=name
                )
            except client.exceptions.ChangeSetNotFoundException:
                change_set = None
            if change_set:
                original = json.loads(change_set["Description"])
                if original["Launcher"] != event["StackId"]:
                    raise InstallationError(
                        "The change set belongs to another launcher. No changes were made."
                    )
                return {
                    "Region": region,
                    "StackId": stack["StackId"],
                    "Baseline": original["Baseline"],
                    "PreserveLegacyPermissions": original.get(
                        "PreserveLegacyPermissions", False
                    ),
                    "Phase": "change_set",
                }
            if stack["StackStatus"] == "REVIEW_IN_PROGRESS":
                raise InstallationError(
                    "An earlier launch left a pending first-installation stack. A new launcher cannot resume its change set. If the original launcher is running, wait. If it failed or was deleted, inspect the target's change sets and resources; delete the target only after confirming the earlier launch has stopped and no resources were deployed, then relaunch. Do not delete an active or populated installation.",
                    stack["StackId"],
                )
            if stack["StackStatus"] not in STABLE_STATES:
                raise InstallationError(
                    "The installation stack is busy or needs recovery. Wait for it to finish or repair it in CloudFormation, then launch a new request.",
                    stack["StackId"],
                )
            for key in ("AccountId", "IAMRoleName", "DatadogSite", "ExternalId"):
                if key == "ExternalId" and not previous.get(key):
                    continue
                if previous.get(key) != props[key]:
                    raise InstallationError(
                        "The existing installation stack belongs to a different account or Datadog integration identity. No changes were made.",
                        stack["StackId"],
                    )
            types |= resource_types(previous.get("InstrumentationResourceTypes", ""))
        role = self.forwarding_role()
        role_tags = {tag["Key"]: tag["Value"] for tag in (role or {}).get("Tags", [])}
        region = self.home
        if stack:
            region = stack["StackId"].split(":")[3]
        elif types & FORWARDED_TYPES:
            region = role_tags.get("datadog:primary-region", self.home)
        if region not in self.cloudformation:
            raise InstallationError(
                "The installation region is not enabled in this account. No changes were made."
            )
        owned_stacks, legacy = self.installation_configuration(stack, region, props)
        forwarding = self.forwarding_plan(owned_stacks, role, region, types, props)
        params = change_set_parameters(
            props, types, forwarding, name, previous if stack else None, legacy
        )
        tags = {tag["Key"]: tag["Value"] for tag in (stack or {}).get("Tags", [])}
        version = os.environ["TEMPLATE_VERSION"]
        outputs = {
            entry["OutputKey"]: entry.get("OutputValue", "")
            for entry in (stack or {}).get("Outputs", [])
        }
        previous_version = outputs.get("InstallationTemplateVersion") or tags.get(
            "datadog:agent-installation-template-version"
        )
        if previous_version and version_tuple(previous_version) > version_tuple(
            version
        ):
            raise InstallationError(
                "This launcher is older than the installation stack. Launch the latest template from Datadog."
            )
        # Changing inherited tags can update the frozen child; adopted roots use the version output instead.
        if not legacy:
            tags["datadog:agent-installation-template-version"] = version
        baseline = fingerprint(stack) if stack else None
        response = self.cloudformation[region].create_change_set(
            StackName=stack["StackId"] if stack else STACK_NAME,
            ChangeSetName=name,
            ClientToken=name,
            ChangeSetType="UPDATE" if stack else "CREATE",
            IncludeNestedStacks=True,
            TemplateURL=os.environ["INSTALLATION_TEMPLATE_URL"],
            Parameters=params,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            Tags=[{"Key": key, "Value": value} for key, value in tags.items()],
            Description=json.dumps(
                {
                    "Launcher": event["StackId"],
                    "Baseline": baseline,
                    "PreserveLegacyPermissions": bool(legacy),
                }
            ),
        )
        return {
            "Region": region,
            "StackId": response["StackId"],
            "Baseline": baseline,
            "PreserveLegacyPermissions": bool(legacy),
            "Phase": "change_set",
        }


def is_noop_change_set(change_set):
    reason = change_set.get("StatusReason", "")
    return change_set["Status"] == "FAILED" and (
        "didn't contain changes" in reason or "No updates are to be performed" in reason
    )


def assert_safe_changes(
    client, change_set_id, is_root=True, preserve_legacy=False, unchanged=False
):
    for page in client.get_paginator("describe_change_set").paginate(
        ChangeSetName=change_set_id
    ):
        if not is_root and is_noop_change_set(page) and not page.get("Changes"):
            continue
        if page["Status"] != "CREATE_COMPLETE" or not page.get("IncludeNestedStacks"):
            raise InstallationError(
                "The installation change set hierarchy is incomplete. Inspect it in CloudFormation before retrying; no changes were executed."
            )
        if unchanged and page.get("Changes"):
            raise InstallationError(
                "The change set would modify the frozen legacy permissions child. No changes were executed."
            )
        for change in page["Changes"]:
            resource = change["ResourceChange"]
            frozen = (
                is_root
                and preserve_legacy
                and resource["LogicalResourceId"] == PERMISSIONS_RESOURCE
            )
            if frozen and resource["Action"] != "Modify":
                raise InstallationError(
                    "The legacy permissions child cannot be added, removed, or replaced during adoption."
                )
            if (
                resource["Action"] not in {"Add", "Modify"}
                or (
                    resource["Action"] == "Modify"
                    and resource.get("Replacement") != "False"
                )
                or (
                    is_root
                    and (
                        resource["LogicalResourceId"] not in INSTALLATION_RESOURCES
                        or resource["ResourceType"] != "AWS::CloudFormation::Stack"
                    )
                )
            ):
                raise InstallationError(
                    "The installation change set would remove, potentially replace, or change an unrecognized resource. Review it manually in CloudFormation; no changes were executed."
                )
            if resource["ResourceType"] == "AWS::CloudFormation::Stack":
                nested_id = resource.get("ChangeSetId")
                if not nested_id:
                    raise InstallationError(
                        "A nested installation change set is missing. Inspect it in CloudFormation before retrying; no changes were executed."
                    )
                assert_safe_changes(client, nested_id, is_root=False, unchanged=frozen)


def poll_change_set(client, event, state):
    name = request_name(event)
    change_set = client.describe_change_set(
        StackName=state["StackId"], ChangeSetName=name
    )
    if change_set["ExecutionStatus"] in {"EXECUTE_IN_PROGRESS", "EXECUTE_COMPLETE"}:
        state["Phase"] = "deployment"
        return False
    if change_set["Status"] in {"CREATE_PENDING", "CREATE_IN_PROGRESS"}:
        return False
    stack = client.describe_stacks(StackName=state["StackId"])["Stacks"][0]
    if state["Baseline"] is not None and (
        fingerprint(stack) != state["Baseline"]
        or stack["StackStatus"] not in STABLE_STATES
    ):
        raise InstallationError(
            "Another request changed the installation stack. Launch a new request to include its latest settings."
        )
    if change_set["Status"] == "FAILED":
        if state["Baseline"] is not None and is_noop_change_set(change_set):
            return True
        raise InstallationError(
            "CloudFormation could not prepare the installation change set. Inspect it in the installation stack; no changes were executed by this request."
        )
    if (
        change_set["Status"] != "CREATE_COMPLETE"
        or change_set["ExecutionStatus"] != "AVAILABLE"
    ):
        raise InstallationError(
            "The installation change set is no longer executable. Launch a new request."
        )
    change_set_id = change_set["ChangeSetId"]
    assert_safe_changes(
        client,
        change_set_id,
        preserve_legacy=state.get("PreserveLegacyPermissions", False),
    )
    client.execute_change_set(
        StackName=state["StackId"],
        ChangeSetName=change_set_id,
        ClientRequestToken=name,
        DisableRollback=False,
    )
    state["Phase"] = "deployment"
    return False


def poll_deployment(client, event, state):
    for page in client.get_paginator("describe_stack_events").paginate(
        StackName=state["StackId"]
    ):
        for item in page["StackEvents"]:
            if item.get("PhysicalResourceId") != state["StackId"] or item.get(
                "ClientRequestToken"
            ) != request_name(event):
                continue
            status = item["ResourceStatus"]
            if status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}:
                return True
            if status in FAILED_STATES:
                raise InstallationError(
                    "The installation deployment failed or rolled back. Inspect the installation stack's Events tab before retrying."
                )
            return False
    return False


def launcher_deployment_completed(client, stack_id):
    for page in client.get_paginator("describe_stack_events").paginate(
        StackName=stack_id
    ):
        for item in page["StackEvents"]:
            if (
                item.get("PhysicalResourceId") != stack_id
                or item.get("ResourceType") != "AWS::CloudFormation::Stack"
            ):
                continue
            status = item["ResourceStatus"]
            if status.startswith("DELETE_"):
                continue
            # Events are newest first; an older success cannot clear a later failed request.
            return status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
    return False


def wait_for_result(event, context, state):
    client = boto3.client(
        "cloudformation", region_name=state["Region"], config=SDK_CONFIG
    )
    while True:
        if time.time() >= state["Deadline"]:
            raise InstallationError(
                "Timed out waiting for the installation. It may still be running; inspect its Events tab before retrying."
            )
        if state["Phase"] == "delete":
            status = client.describe_stacks(StackName=state["StackId"])["Stacks"][0][
                "StackStatus"
            ]
            done = not status.endswith("_IN_PROGRESS") or status == "REVIEW_IN_PROGRESS"
        elif state["Phase"] == "change_set":
            done = poll_change_set(client, event, state)
        else:
            done = poll_deployment(client, event, state)
        if done:
            send_cfn_response(
                cfnresponse,
                event,
                context,
                cfnresponse.SUCCESS,
                {
                    "InstallationStackId": state["StackId"],
                    "InstallationStackURL": stack_url(state["StackId"]),
                    "Result": "Launcher removed; installation preserved"
                    if state["Phase"] == "delete"
                    else "Installation deployment completed",
                },
            )
            return
        if context.get_remaining_time_in_millis() < 90_000:
            continuation = {**event, "LauncherState": state}
            continuation["ResourceProperties"] = {
                "AccountId": event["ResourceProperties"]["AccountId"]
            }
            continuation.pop("OldResourceProperties", None)
            boto3.client("lambda", config=SDK_CONFIG).invoke(
                FunctionName=context.invoked_function_arn,
                InvocationType="Event",
                Payload=json.dumps(continuation).encode(),
            )
            return
        time.sleep(5)


def handler(event, context):
    state = event.get("LauncherState")
    try:
        partition, _, _, account = event["StackId"].split(":")[1:5]
        if event["ResourceProperties"]["AccountId"] != account:
            raise InstallationError(
                "Launch this template in the AWS account selected in Datadog."
            )
        if partition != "aws" and event["RequestType"] != "Delete":
            raise InstallationError(
                "This installation launcher supports only commercial AWS accounts. AWS GovCloud and China are not supported."
            )
        if not state:
            deadline = request_deadline(event)
            if time.time() >= deadline:
                raise InstallationError(
                    "This launcher request has expired. No new deployment was started; launch a new request."
                )
            if event["RequestType"] == "Delete":
                client = boto3.client(
                    "cloudformation",
                    region_name=event["StackId"].split(":")[3],
                    config=SDK_CONFIG,
                )
                if launcher_deployment_completed(client, event["StackId"]):
                    send_cfn_response(
                        cfnresponse,
                        event,
                        context,
                        cfnresponse.SUCCESS,
                        {"Result": "Launcher removed; installation preserved"},
                    )
                    return
                stack = Launcher(boto3.Session(), partition).find_stack()
                if not stack:
                    send_cfn_response(
                        cfnresponse,
                        event,
                        context,
                        cfnresponse.SUCCESS,
                        {"Result": "Launcher removed; no installation was deleted"},
                    )
                    return
                state = {
                    "Region": stack["StackId"].split(":")[3],
                    "StackId": stack["StackId"],
                    "Phase": "delete",
                }
            else:
                state = Launcher(boto3.Session(), partition).prepare(event)
            state["Deadline"] = deadline
        wait_for_result(event, context, state)
    except Exception as error:  # noqa: BLE001 - CloudFormation needs a response even for unexpected failures.
        # AWS exception text and resource properties can contain NoEcho values or signed response URLs.
        message = "AWS could not complete this launcher request. Check the launcher's deployment permissions and the installation stack in CloudFormation."
        if isinstance(error, InstallationError):
            message = str(error)
        elif isinstance(error, ClientError):
            code = error.response["Error"].get("Code", "")
            operation = error.operation_name
            if re.fullmatch(r"[A-Za-z0-9]+", code) and re.fullmatch(
                r"[A-Za-z0-9]+", operation
            ):
                message = f"AWS {operation} failed ({code}). Check the launcher's deployment permissions and the installation stack in CloudFormation."
        target_id = state["StackId"] if state else getattr(error, "stack_id", None)
        target_url = stack_url(target_id) if target_id else None
        if target_url:
            message = f"{message} Stack: {target_url}"
        data = {"Message": message}
        if target_url:
            data["InstallationStackURL"] = target_url
        send_cfn_response(
            cfnresponse, event, context, cfnresponse.FAILED, data, reason=message
        )
