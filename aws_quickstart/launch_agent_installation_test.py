import json
import os
import sys
import unittest
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from importlib import import_module
from itertools import product
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import yaml
from botocore.exceptions import ClientError
from botocore.stub import Stubber

sys.modules.setdefault("cfnresponse", MagicMock(SUCCESS="SUCCESS", FAILED="FAILED"))
launcher = import_module("launch_agent_installation")


ACCOUNT = "123456789012"
ROLE_CREATED = datetime(2026, 1, 1, tzinfo=UTC)
ROLE_RECORDED = datetime(2026, 1, 2, tzinfo=UTC)
ROOT = (
    f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/DatadogAgentInstallation/target"
)
REQUEST = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/DatadogAgentInstallationLauncher-request/launcher"
CHANGE_SET = (
    f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:changeSet/datadog-agent-request/root"
)
NESTED_CHANGE_SET = (
    f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:changeSet/nested-request/child"
)
FORWARDING = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/DatadogAgentInstallation-forwarding/nested"
SUPPLEMENTARY = FORWARDING.replace(
    "forwarding/nested", "supplementary/01234567-89ab-cdef-0123-456789abcdef"
)
DESTINATION_ARN = f"arn:aws:events:us-east-1:{ACCOUNT}:api-destination/{launcher.DESTINATION}/destination-id"
SUPPLEMENTARY_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/supplementary-invoker"
INVOCATION_ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/invocation-role"
QUICKSTART_ROOT = ROOT.replace("DatadogAgentInstallation/", "DatadogIntegration/")
QUICKSTART_FORWARDING = FORWARDING.replace(
    "DatadogAgentInstallation-", "DatadogIntegration-"
)
TEMPLATE_URL = (
    "https://test-bucket.s3.amazonaws.com/aws/v4.23.0/main_agent_installation.yaml"
)
PROPS = {
    "AccountId": ACCOUNT,
    "IAMRoleName": "DatadogIntegrationRole",
    "DatadogSite": "datadoghq.com",
    "ExternalId": "current-org-external-id",
    "InstrumentationResourceTypes": "aws:ec2:instance",
    "APIKey": "test-api-secret",
}


class MissingResource(Exception):
    pass


def event(kind="Create"):
    return {
        "RequestType": kind,
        "StackId": REQUEST,
        "RequestId": "request-one",
        "LogicalResourceId": "LaunchInstallation",
        "ResponseURL": "https://example.invalid/signed-response",
        "ResourceProperties": dict(PROPS),
    }


def stack(**overrides):
    result = {
        "StackId": ROOT,
        "StackStatus": "CREATE_COMPLETE",
        "CreationTime": "initial",
        "Parameters": [
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in PROPS.items()
        ],
        "Tags": [],
    }
    result.update(overrides)
    return result


def state(phase="change_set", baseline="initial"):
    return {
        "Region": "us-east-1",
        "StackId": ROOT,
        "Baseline": baseline,
        "Phase": phase,
        "Deadline": 1000,
    }


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {"INSTALLATION_TEMPLATE_URL": TEMPLATE_URL, "TEMPLATE_VERSION": "v4.23.0"},
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = Mock()
        self.iam = Mock(
            exceptions=SimpleNamespace(NoSuchEntityException=MissingResource)
        )
        self.ec2 = Mock()
        self.cf = Mock(
            exceptions=SimpleNamespace(ChangeSetNotFoundException=MissingResource)
        )
        self.events = Mock(
            exceptions=SimpleNamespace(ResourceNotFoundException=MissingResource)
        )
        self.session.client.side_effect = lambda service, **kwargs: {
            "iam": self.iam,
            "ec2": self.ec2,
            "cloudformation": self.cf,
            "events": self.events,
        }[service]
        self.ec2.describe_regions.return_value = {
            "Regions": [{"RegionName": "us-east-1"}]
        }
        self.subject = launcher.Launcher(self.session, "aws")
        self.current_stack = None
        self.supplementary_stack = None
        self.supplementary_resources = []
        self.forwarding_invocation_resources = []
        self.permissions_scope = "stack"
        self.owner_identity = dict(PROPS)
        self.role_owners = {}
        self.cf.describe_stacks.side_effect = self.describe_stacks
        self.cf.describe_change_set.side_effect = MissingResource()
        self.cf.create_change_set.return_value = {"StackId": ROOT}
        self.cf.describe_stack_resources.return_value = {
            "StackResources": [
                {
                    "LogicalResourceId": "DatadogIntegrationPermissionsStack",
                    "PhysicalResourceId": "permissions",
                    "ResourceType": "AWS::CloudFormation::Stack",
                }
            ]
        }
        self.cf.describe_stack_resources.side_effect = self.describe_stack_resources
        self.iam.get_role.side_effect = MissingResource()
        self.events.describe_connection.side_effect = MissingResource()
        self.events.describe_api_destination.side_effect = MissingResource()
        self.events.list_rules.return_value = {"Rules": []}
        self.events.get_paginator.return_value.paginate.return_value = [{"Rules": []}]

    def describe_stack_resources(
        self, *, StackName=None, PhysicalResourceId=None, LogicalResourceId=None
    ):
        if PhysicalResourceId is not None:
            owner = self.role_owners.get(PhysicalResourceId)
            return {
                "StackResources": [
                    {
                        "StackId": owner,
                        "LogicalResourceId": LogicalResourceId,
                        "PhysicalResourceId": PhysicalResourceId,
                        "ResourceType": "AWS::IAM::Role",
                        "ResourceStatus": "CREATE_COMPLETE",
                        "Timestamp": ROLE_RECORDED,
                    }
                ]
                if owner
                else []
            }
        if (
            StackName in self.role_owners.values()
            and LogicalResourceId == "DDEventBridgeInvocationRole"
        ):
            return {"StackResources": self.forwarding_invocation_resources}
        if StackName == QUICKSTART_ROOT:
            return {
                "StackResources": [{"LogicalResourceId": "DatadogIntegrationRoleStack"}]
            }
        if StackName == SUPPLEMENTARY:
            return {"StackResources": self.supplementary_resources}
        return self.cf.describe_stack_resources.return_value

    def describe_stacks(self, *, StackName):
        if StackName == SUPPLEMENTARY:
            return {"Stacks": [self.supplementary_stack]}
        if StackName == FORWARDING:
            return {"Stacks": [stack(StackId=FORWARDING, RootId=ROOT, ParentId=ROOT)]}
        if StackName in {QUICKSTART_ROOT, QUICKSTART_FORWARDING}:
            return {
                "Stacks": [
                    stack(
                        StackId=StackName,
                        RootId=QUICKSTART_ROOT,
                        Parameters=[
                            {"ParameterKey": key, "ParameterValue": value}
                            for key, value in self.owner_identity.items()
                        ],
                    )
                ]
            }
        if StackName == "permissions":
            return {
                "Stacks": [
                    stack(
                        Parameters=[
                            {
                                "ParameterKey": "InstrumentationPolicyScope",
                                "ParameterValue": self.permissions_scope,
                            }
                        ]
                    )
                ]
            }
        if self.current_stack is not None:
            return {"Stacks": [self.current_stack]}
        raise ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": "Stack with id DatadogAgentInstallation does not exist",
                }
            },
            "DescribeStacks",
        )

    def existing(self, **overrides):
        self.current_stack = stack(**overrides)
        self.cf.describe_stacks.side_effect = self.describe_stacks
        self.cf.describe_stacks.return_value = {"Stacks": [self.current_stack]}

    def forwarding(self, owner=QUICKSTART_FORWARDING):
        tags = [{"Key": "datadog:primary-region", "Value": "us-east-1"}]
        self.role_owners[launcher.FORWARDING_ROLE] = owner
        self.forwarding_invocation_resources = [
            {
                "StackId": owner,
                "LogicalResourceId": "DDEventBridgeInvocationRole",
                "PhysicalResourceId": "invocation-role",
                "ResourceType": "AWS::IAM::Role",
                "ResourceStatus": "CREATE_COMPLETE",
            }
        ]
        self.iam.get_role.side_effect = None
        self.iam.get_role.return_value = {
            "Role": {
                "RoleName": launcher.FORWARDING_ROLE,
                "CreateDate": ROLE_CREATED,
                "Tags": tags,
            }
        }
        self.events.describe_connection.side_effect = None
        self.events.describe_connection.return_value = {
            "ConnectionState": "AUTHORIZED",
            "ConnectionArn": "connection",
        }
        self.events.describe_api_destination.side_effect = None
        self.events.describe_api_destination.return_value = {
            "ApiDestinationState": "ACTIVE",
            "ApiDestinationArn": DESTINATION_ARN,
            "ConnectionArn": "connection",
            "InvocationEndpoint": "https://api.datadoghq.com/api/unstable/instrumenter/events",
            "HttpMethod": "POST",
        }
        self.events.describe_rule.return_value = {
            "State": "ENABLED",
            "EventPattern": json.dumps(launcher.expected_pattern("aws:ec2:instance")),
        }
        self.events.list_targets_by_rule.return_value = {
            "Targets": [{"Arn": DESTINATION_ARN, "RoleArn": INVOCATION_ROLE_ARN}]
        }
        self.events.list_tags_for_resource.return_value = {
            "Tags": [{"Key": launcher.SUPPLEMENTARY_OWNER_TAG, "Value": SUPPLEMENTARY}]
        }

    def supplementary(self, *types):
        self.current_stack["Parameters"].extend(
            [
                {
                    "ParameterKey": "SupplementaryResourceTypes",
                    "ParameterValue": ",".join(types),
                },
                {
                    "ParameterKey": "ExistingApiDestinationArn",
                    "ParameterValue": DESTINATION_ARN,
                },
            ]
        )
        self.supplementary_stack = stack(
            StackId=SUPPLEMENTARY,
            Parameters=[
                {
                    "ParameterKey": "InstrumentationResourceTypes",
                    "ParameterValue": ",".join(types),
                },
                {
                    "ParameterKey": "ExistingApiDestinationArn",
                    "ParameterValue": DESTINATION_ARN,
                },
            ],
        )
        self.supplementary_resources = [
            {
                "LogicalResourceId": "DDEventBridgeInvocationRole",
                "PhysicalResourceId": "supplementary-invoker",
                "ResourceType": "AWS::IAM::Role",
            }
        ]
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.SUPPLEMENTARY_RESOURCE,
                "PhysicalResourceId": SUPPLEMENTARY,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        self.events.get_paginator.return_value.paginate.return_value = [
            {
                "Rules": [
                    {"Name": launcher.supplementary_rule_name(SUPPLEMENTARY, rt)}
                    for rt in types
                ]
            }
        ]

    def coverage(self, *external_types):
        def describe(*, Name):
            service = Name.rsplit("-", 1)[1]
            rt = next(
                rt for rt in launcher.FORWARDED_TYPES if rt.split(":")[1] == service
            )
            if Name.startswith(launcher.RULE_PREFIX) and rt not in external_types:
                raise MissingResource()
            return {
                "Arn": f"arn:aws:events:us-east-1:{ACCOUNT}:rule/{Name}",
                "State": "ENABLED",
                "EventPattern": json.dumps(launcher.expected_pattern(rt)),
            }

        self.events.describe_rule.side_effect = describe
        self.events.list_targets_by_rule.side_effect = lambda *, Rule: {
            "Targets": [
                {
                    "Arn": DESTINATION_ARN,
                    "RoleArn": SUPPLEMENTARY_ROLE_ARN
                    if Rule.startswith(launcher.SUPPLEMENTARY_RULE_PREFIX)
                    else INVOCATION_ROLE_ARN,
                }
            ]
        }

    def values(self):
        return {
            param["ParameterKey"]: param
            for param in self.cf.create_change_set.call_args.kwargs["Parameters"]
        }

    def test_create_without_forwarding(self):
        result = self.subject.prepare(event())
        args = self.cf.create_change_set.call_args.kwargs
        self.assertEqual(args["ChangeSetType"], "CREATE")
        self.assertEqual(args["StackName"], "DatadogAgentInstallation")
        self.assertEqual(args["TemplateURL"], TEMPLATE_URL)
        self.assertTrue(args["IncludeNestedStacks"])
        self.assertNotIn("RoleARN", args)
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "true"
        )
        self.assertEqual(result["StackId"], ROOT)
        self.assertIsNone(result["Baseline"])

    def test_external_forwarding_is_not_recreated(self):
        owner = ROOT.replace(
            "DatadogAgentInstallation/target", "DatadogIntegration-forwarding/nested"
        )
        self.forwarding(owner)
        self.cf.describe_stacks.side_effect = None
        self.cf.describe_stacks.return_value = {
            "Stacks": [stack(StackId=owner, RootId="quickstart-root")]
        }
        self.cf.describe_stack_resources.return_value = {
            "StackResources": [{"LogicalResourceId": "DatadogIntegrationRoleStack"}]
        }
        with patch.object(self.subject, "find_stack", return_value=None):
            self.subject.prepare(event())
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.events.put_rule.assert_not_called()
        self.iam.put_role_policy.assert_not_called()

    def test_update_merges_types_and_preserves_secrets_other_parameters_and_tags(self):
        current = stack(Tags=[{"Key": "owner", "Value": "customer"}])
        current["Parameters"].extend(
            [
                {"ParameterKey": "FutureSetting", "ParameterValue": "keep"},
                {"ParameterKey": "APPKey", "ParameterValue": "old-app-secret"},
            ]
        )
        self.existing(**current)
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:eks:cluster"
        )
        self.subject.prepare(request)
        args = self.cf.create_change_set.call_args.kwargs
        self.assertEqual(args["ChangeSetType"], "UPDATE")
        self.assertEqual(
            self.values()["InstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance,aws:eks:cluster",
        )
        self.assertNotIn("APPKey", self.values())
        for key in ("APIKey", "FutureSetting"):
            self.assertEqual(
                self.values()[key], {"ParameterKey": key, "UsePreviousValue": True}
            )
        self.assertIn({"Key": "owner", "Value": "customer"}, args["Tags"])
        self.assertEqual(json.loads(args["Description"])["Baseline"], "initial")

    def test_each_request_refreshes_permissions_even_for_unchanged_types(self):
        self.existing()
        self.subject.prepare(event())
        first_token = self.values()["InstrumentationRefreshToken"]["ParameterValue"]
        second = event()
        second["RequestId"] = "second-request"
        self.subject.prepare(second)
        self.assertNotEqual(
            self.values()["InstrumentationRefreshToken"]["ParameterValue"], first_token
        )
        self.assertEqual(
            self.values()["InstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance",
        )

    def test_owned_forwarding_remains_owned_and_can_add_new_types(self):
        self.existing()
        self.forwarding(FORWARDING)
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceStatus": "CREATE_COMPLETE",
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:eks:cluster"
        )
        self.subject.prepare(request)
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "true"
        )
        self.events.describe_rule.assert_not_called()

    def test_missing_owned_role_is_not_treated_as_absence(self):
        self.existing()
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        with self.assertRaisesRegex(launcher.InstallationError, "role is missing"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_forwarding_owner_must_match(self):
        self.existing()
        self.forwarding(QUICKSTART_FORWARDING)
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        with self.assertRaisesRegex(launcher.InstallationError, "ownership"):
            self.subject.prepare(event())

    def test_renamed_installation_owner_cannot_be_bypassed(self):
        owner = ROOT.replace(
            "DatadogAgentInstallation/target",
            "DatadogAgentInstallation2-forwarding/nested",
        )
        self.cf.describe_stacks.side_effect = None
        self.cf.describe_stacks.return_value = {
            "Stacks": [stack(StackId=owner, RootId="renamed-installation-root")]
        }
        with self.assertRaisesRegex(
            launcher.InstallationError, "Another installation stack"
        ):
            self.subject.assert_external_owner(owner)
        self.cf.create_change_set.assert_not_called()

    def test_missing_external_coverage_creates_only_supplementary_rules(self):
        self.forwarding()
        self.coverage("aws:ec2:instance")
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ec2:instance,aws:eks:cluster"
        )
        self.subject.prepare(request)
        values = self.values()
        self.assertEqual(
            values["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.assertEqual(
            values["SupplementaryResourceTypes"]["ParameterValue"], "aws:eks:cluster"
        )
        self.assertEqual(
            values["ExistingApiDestinationArn"]["ParameterValue"], DESTINATION_ARN
        )
        self.events.put_rule.assert_not_called()
        self.events.update_api_destination.assert_not_called()
        self.events.update_connection.assert_not_called()
        self.iam.put_role_policy.assert_not_called()

    def test_all_missing_external_types_are_supplemented(self):
        self.forwarding()
        self.coverage()
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = ",".join(
            sorted(launcher.FORWARDED_TYPES)
        )
        self.subject.prepare(request)
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"],
            request["ResourceProperties"]["InstrumentationResourceTypes"],
        )

    def test_complete_external_coverage_needs_no_supplementary_stack(self):
        self.forwarding()
        self.coverage(*launcher.FORWARDED_TYPES)
        self.subject.prepare(event())
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"], ""
        )
        self.assertEqual(
            self.values()["ExistingApiDestinationArn"]["ParameterValue"], ""
        )

    def test_supplementary_types_survive_relaunch_and_later_quickstart_overlap(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        for external_types in ((), ("aws:ec2:instance",)):
            with self.subTest(external_types=external_types):
                self.coverage(*external_types)
                self.subject.prepare(event())
                self.assertEqual(
                    self.values()["SupplementaryResourceTypes"]["ParameterValue"],
                    "aws:ec2:instance",
                )
                self.assertEqual(
                    self.values()["APIKey"],
                    {"ParameterKey": "APIKey", "UsePreviousValue": True},
                )
        self.events.delete_rule.assert_not_called()
        self.events.disable_rule.assert_not_called()

    def test_owned_supplementary_coverage_does_not_require_healthy_canonical_rules(
        self,
    ):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage("aws:ec2:instance")
        describe = self.events.describe_rule.side_effect
        canonical_name = launcher.RULE_PREFIX + "ec2"

        def disabled_canonical_rule(*, Name):
            result = describe(Name=Name)
            if Name == canonical_name:
                result["State"] = "DISABLED"
            return result

        self.events.describe_rule.side_effect = disabled_canonical_rule
        for requested in ("aws:ec2:instance", "aws:eks:cluster"):
            with self.subTest(requested=requested):
                request = event()
                request["ResourceProperties"]["InstrumentationResourceTypes"] = (
                    requested
                )
                self.subject.prepare(request)
                expected_types = sorted({"aws:ec2:instance", requested})
                self.assertEqual(
                    self.values()["SupplementaryResourceTypes"]["ParameterValue"],
                    ",".join(expected_types),
                )
        self.assertNotIn(
            canonical_name,
            [call.kwargs["Name"] for call in self.events.describe_rule.call_args_list],
        )
        self.events.put_rule.assert_not_called()
        self.events.disable_rule.assert_not_called()

    def test_supplementary_coverage_does_not_bypass_another_types_disabled_rule(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage("aws:ec2:instance", "aws:eks:cluster")
        describe = self.events.describe_rule.side_effect

        def disabled_eks_rule(*, Name):
            result = describe(Name=Name)
            if Name == launcher.RULE_PREFIX + "eks":
                result["State"] = "DISABLED"
            return result

        self.events.describe_rule.side_effect = disabled_eks_rule
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:eks:cluster"
        )
        with self.assertRaisesRegex(launcher.InstallationError, "aws:eks:cluster"):
            self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_coverage_still_requires_a_healthy_shared_pipeline(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage()
        self.events.describe_api_destination.return_value["ApiDestinationState"] = (
            "INACTIVE"
        )
        with self.assertRaisesRegex(
            launcher.InstallationError, "forwarding is incomplete"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_update_merges_owned_and_new_types(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage("aws:ec2:instance")
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:eks:cluster"
        )
        self.subject.prepare(request)
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"],
            "aws:ec2:instance,aws:eks:cluster",
        )
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )

    def test_renamed_or_orphaned_supplementary_rules_are_not_adopted(self):
        self.forwarding()
        self.events.get_paginator.return_value.paginate.return_value = [
            {"Rules": []},
            {"Rules": [{"Name": "datadog-agent-supp-another-owner-eks"}]},
        ]
        with self.assertRaisesRegex(
            launcher.InstallationError, "unrecognized ownership"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_missing_or_incompatible_owned_supplementary_resources_block_updates(self):
        for case in (
            "missing-rule",
            "wrong-destination",
            "wrong-role",
            "missing-role",
            "wrong-types",
        ):
            with self.subTest(case=case):
                self.existing()
                self.forwarding()
                self.cf.describe_stack_resources.return_value["StackResources"] = (
                    self.cf.describe_stack_resources.return_value["StackResources"][:1]
                )
                self.supplementary("aws:ec2:instance")
                self.coverage("aws:ec2:instance")
                if case == "missing-rule":
                    self.events.get_paginator.return_value.paginate.return_value = [
                        {"Rules": []}
                    ]
                elif case == "wrong-destination":
                    self.supplementary_stack["Parameters"][1]["ParameterValue"] = (
                        "different"
                    )
                elif case == "wrong-role":
                    self.events.list_targets_by_rule.side_effect = None
                elif case == "missing-role":
                    self.supplementary_resources.clear()
                else:
                    self.supplementary_stack["Parameters"][0]["ParameterValue"] = (
                        "aws:ecs:cluster"
                    )
                with self.assertRaises(launcher.InstallationError):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_owner_tag_cannot_be_missing_or_foreign(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage("aws:ec2:instance")
        for tags in (
            [],
            [{"Key": launcher.SUPPLEMENTARY_OWNER_TAG, "Value": "another-stack"}],
        ):
            with self.subTest(tags=tags):
                self.events.list_tags_for_resource.return_value = {"Tags": tags}
                with self.assertRaisesRegex(
                    launcher.InstallationError, "does not belong"
                ):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_owned_supplementary_discovery_denials_are_not_ignored(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage("aws:ec2:instance")
        for method in (
            self.events.describe_rule,
            self.events.list_targets_by_rule,
            self.events.list_tags_for_resource,
        ):
            with self.subTest(method=method):
                original = method.side_effect
                method.side_effect = ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DescribeRule",
                )
                with self.assertRaises(ClientError):
                    self.subject.prepare(event())
                method.side_effect = original
        self.cf.create_change_set.assert_not_called()

    def test_missing_supplementary_nested_stack_is_not_recreated(self):
        self.existing()
        self.current_stack["Parameters"].append(
            {
                "ParameterKey": "SupplementaryResourceTypes",
                "ParameterValue": "aws:ec2:instance",
            }
        )
        self.forwarding()
        with self.assertRaisesRegex(
            launcher.InstallationError, "ownership is inconsistent"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_missing_shared_role_does_not_create_a_pipeline_for_supplementary_stack(
        self,
    ):
        self.existing()
        self.supplementary("aws:ec2:instance")
        with self.assertRaisesRegex(launcher.InstallationError, "role is missing"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_orphaned_supplementary_rules_block_full_pipeline_creation(self):
        self.events.list_rules.side_effect = lambda **kwargs: {
            "Rules": [{"Name": "datadog-agent-supp-orphan-eks"}]
            if kwargs["NamePrefix"] == launcher.SUPPLEMENTARY_RULE_PREFIX
            else []
        }
        with self.assertRaisesRegex(
            launcher.InstallationError, "without their forwarding role"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_existing_installation_is_not_moved_to_add_supplementary_rules(self):
        self.existing(StackId=ROOT.replace("us-east-1", "eu-west-1"))
        self.subject.cloudformation["eu-west-1"] = self.cf
        self.forwarding()
        self.coverage()
        with self.assertRaisesRegex(launcher.InstallationError, "primary region"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_new_installation_uses_external_primary_region_for_supplementary_rules(
        self,
    ):
        self.forwarding()
        self.coverage()
        self.subject.cloudformation["eu-west-1"] = self.cf
        self.subject.events["eu-west-1"] = self.events
        self.iam.get_role.return_value["Role"]["Tags"][0]["Value"] = "eu-west-1"
        destination = DESTINATION_ARN.replace("us-east-1", "eu-west-1")
        self.events.describe_api_destination.return_value["ApiDestinationArn"] = (
            destination
        )
        result = self.subject.prepare(event())
        self.assertEqual(result["Region"], "eu-west-1")
        self.assertEqual(
            self.values()["ExistingApiDestinationArn"]["ParameterValue"], destination
        )

    def test_unsupported_primary_region_does_not_create_supplementary_rules(self):
        self.forwarding()
        self.iam.get_role.return_value["Role"]["Tags"][0]["Value"] = "us-gov-west-1"
        self.subject.cloudformation["us-gov-west-1"] = self.cf
        with self.assertRaisesRegex(
            launcher.InstallationError, "supported, enabled primary region"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_sdk_pagination_detects_other_supplementary_owners(self):
        self.forwarding()
        client = launcher.boto3.client(
            "events",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        self.subject.events["us-east-1"] = client
        with Stubber(client) as stub:
            stub.add_response(
                "list_rules",
                {"Rules": [], "NextToken": "next"},
                {"NamePrefix": launcher.SUPPLEMENTARY_RULE_PREFIX},
            )
            stub.add_response(
                "list_rules",
                {"Rules": [{"Name": "datadog-agent-supp-another-eks"}]},
                {"NamePrefix": launcher.SUPPLEMENTARY_RULE_PREFIX, "NextToken": "next"},
            )
            with self.assertRaisesRegex(
                launcher.InstallationError, "unrecognized ownership"
            ):
                self.subject.supplementary_types(
                    None,
                    "us-east-1",
                    launcher.FORWARDED_TYPES,
                    DESTINATION_ARN,
                    ACCOUNT,
                )
            stub.assert_no_pending_responses()

    def test_supplementary_discovery_denial_does_not_mean_missing_coverage(self):
        self.forwarding()
        self.events.get_paginator.return_value.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListRules"
        )
        with self.assertRaises(ClientError):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_canonical_forwarding_resolves_its_invocation_role_once(self):
        self.forwarding()
        self.coverage("aws:ec2:instance", "aws:lambda:function")
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ec2:instance,aws:lambda:function"
        )
        self.subject.prepare(request)
        lookups = [
            call.kwargs
            for call in self.cf.describe_stack_resources.call_args_list
            if call.kwargs.get("LogicalResourceId") == "DDEventBridgeInvocationRole"
        ]
        self.assertEqual(
            lookups,
            [
                {
                    "StackName": QUICKSTART_FORWARDING,
                    "LogicalResourceId": "DDEventBridgeInvocationRole",
                }
            ],
        )
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"], ""
        )

    def test_canonical_invocation_role_uses_its_owner_region_and_physical_name(self):
        self.forwarding()
        owner = QUICKSTART_FORWARDING.replace(":us-east-1:", ":us-west-2:")
        client = Mock()
        client.describe_stack_resources.return_value = {
            "StackResources": [
                {
                    **self.forwarding_invocation_resources[0],
                    "StackId": owner,
                    "PhysicalResourceId": "GeneratedForwardingInvocationRole",
                    "ResourceStatus": "UPDATE_COMPLETE",
                }
            ]
        }
        self.subject.cloudformation["us-west-2"] = client
        self.assertEqual(
            self.subject.forwarding_invocation_role(owner, ACCOUNT),
            f"arn:aws:iam::{ACCOUNT}:role/GeneratedForwardingInvocationRole",
        )
        client.describe_stack_resources.assert_called_once_with(
            StackName=owner, LogicalResourceId="DDEventBridgeInvocationRole"
        )
        self.cf.describe_stack_resources.assert_not_called()

    def test_canonical_forwarding_rejects_another_invocation_role(self):
        self.forwarding()
        for role_arn in (
            f"arn:aws:iam::{ACCOUNT}:role/{launcher.FORWARDING_ROLE}",
            f"arn:aws:iam::{ACCOUNT}:role/another-invoker",
            "arn:aws:iam::999999999999:role/invocation-role",
        ):
            with self.subTest(role_arn=role_arn):
                self.events.list_targets_by_rule.return_value["Targets"][0][
                    "RoleArn"
                ] = role_arn
                with self.assertRaisesRegex(launcher.InstallationError, "incompatible"):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_canonical_forwarding_requires_a_verified_invocation_resource(self):
        self.forwarding()
        valid = self.forwarding_invocation_resources[0]
        invalid = [[], [valid, valid]] + [
            [{**valid, key: value}]
            for key, value in (
                ("StackId", FORWARDING),
                ("LogicalResourceId", "UnrelatedRole"),
                ("ResourceType", "AWS::IAM::Policy"),
                ("ResourceStatus", "DELETE_COMPLETE"),
                ("ResourceStatus", "UPDATE_IN_PROGRESS"),
                ("PhysicalResourceId", ""),
                ("PhysicalResourceId", None),
                ("PhysicalResourceId", "unexpected/path"),
            )
        ]
        for resources in invalid:
            with self.subTest(resources=resources):
                self.forwarding_invocation_resources = resources
                with self.assertRaisesRegex(
                    launcher.InstallationError, "invocation role"
                ):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_canonical_invocation_role_lookup_denial_fails_closed(self):
        self.forwarding()
        describe = self.cf.describe_stack_resources.side_effect

        def denied(**kwargs):
            if kwargs.get("LogicalResourceId") == "DDEventBridgeInvocationRole":
                raise ClientError(
                    {"Error": {"Code": "AccessDenied"}}, "DescribeStackResources"
                )
            return describe(**kwargs)

        self.cf.describe_stack_resources.side_effect = denied
        with self.assertRaises(ClientError):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_only_coverage_does_not_require_canonical_role(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.coverage()
        self.forwarding_invocation_resources = []
        self.subject.prepare(event())
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"],
            "aws:ec2:instance",
        )
        self.assertFalse(
            any(
                call.kwargs.get("LogicalResourceId") == "DDEventBridgeInvocationRole"
                for call in self.cf.describe_stack_resources.call_args_list
            )
        )

    def test_payload_changing_targets_are_not_reused(self):
        self.forwarding()
        for override in (
            {"Input": '{"replaced":true}'},
            {"InputPath": "$.detail"},
            {"InputTransformer": {"InputTemplate": '"replaced"'}},
        ):
            with self.subTest(override=override):
                self.events.list_targets_by_rule.return_value = {
                    "Targets": [
                        {
                            "Arn": DESTINATION_ARN,
                            "RoleArn": INVOCATION_ROLE_ARN,
                            **override,
                        }
                    ]
                }
                with self.assertRaisesRegex(launcher.InstallationError, "incompatible"):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_payload_changing_supplementary_targets_are_not_reused(self):
        self.forwarding()
        self.events.describe_rule.return_value["Arn"] = "supplementary-rule-arn"
        for override in (
            {"Input": '{"replaced":true}'},
            {"InputPath": "$.detail"},
            {"InputTransformer": {"InputTemplate": '"replaced"'}},
        ):
            with self.subTest(override=override):
                self.events.list_targets_by_rule.return_value = {
                    "Targets": [
                        {
                            "Arn": DESTINATION_ARN,
                            "RoleArn": SUPPLEMENTARY_ROLE_ARN,
                            **override,
                        }
                    ]
                }
                with self.assertRaisesRegex(launcher.InstallationError, "incompatible"):
                    self.subject.rule_covers(
                        "us-east-1",
                        "supplementary-rule",
                        "aws:ec2:instance",
                        DESTINATION_ARN,
                        role_arn=SUPPLEMENTARY_ROLE_ARN,
                        owner_id=SUPPLEMENTARY,
                    )
        self.cf.create_change_set.assert_not_called()

    def test_unrelated_target_transform_does_not_hide_valid_coverage(self):
        self.forwarding()
        self.events.list_targets_by_rule.return_value["Targets"].append(
            {"Arn": "unrelated-target", "Input": '{"replaced":true}'}
        )
        self.subject.prepare(event())
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.assertEqual(
            self.values()["SupplementaryResourceTypes"]["ParameterValue"], ""
        )

    def test_disappearing_external_target_is_not_missing_rule_coverage(self):
        self.forwarding()
        self.events.list_targets_by_rule.side_effect = MissingResource()
        with self.assertRaises(MissingResource):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_destination_must_be_in_account_and_region(self):
        self.forwarding()
        self.coverage()
        for arn in (
            DESTINATION_ARN.replace(ACCOUNT, "999999999999"),
            DESTINATION_ARN.replace("us-east-1", "eu-west-1"),
        ):
            with self.subTest(arn=arn):
                self.events.describe_api_destination.return_value[
                    "ApiDestinationArn"
                ] = arn
                with self.assertRaisesRegex(
                    launcher.InstallationError, "destination ARN"
                ):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_supplementary_and_full_forwarding_cannot_both_be_owned(self):
        self.existing()
        self.forwarding()
        self.supplementary("aws:ec2:instance")
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        with self.assertRaisesRegex(
            launcher.InstallationError, "ownership is inconsistent"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_incompatible_external_configuration_blocks_changes(self):
        for kind in (
            "site",
            "connection",
            "pattern",
            "target",
            "disabled",
            "pagination",
        ):
            with self.subTest(kind=kind):
                self.forwarding()
                if kind == "site":
                    self.events.describe_api_destination.return_value[
                        "InvocationEndpoint"
                    ] = "https://api.datadoghq.eu/api/unstable/instrumenter/events"
                elif kind == "connection":
                    self.events.describe_connection.return_value["ConnectionState"] = (
                        "DEAUTHORIZED"
                    )
                elif kind == "pattern":
                    self.events.describe_rule.return_value["EventPattern"] = "{}"
                elif kind == "target":
                    self.events.list_targets_by_rule.return_value["Targets"] = []
                elif kind == "disabled":
                    self.events.describe_rule.return_value["State"] = "DISABLED"
                else:
                    self.events.list_targets_by_rule.return_value["NextToken"] = "more"
                with self.assertRaises(launcher.InstallationError):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_orphaned_resources_block_creation(self):
        self.events.list_rules.return_value = {"Rules": [{"Name": "leftover"}]}
        with self.assertRaisesRegex(
            launcher.InstallationError, "without their forwarding role"
        ):
            self.subject.prepare(event())

    def test_aws_denial_is_never_absence(self):
        denied = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "DescribeStacks"
        )
        for method in (self.cf.describe_stacks, self.events.describe_connection):
            with self.subTest(method=method):
                old = method.side_effect
                method.side_effect = denied
                with self.assertRaises(ClientError):
                    self.subject.prepare(event())
                method.side_effect = old
        self.cf.create_change_set.assert_not_called()

    def test_foreign_stack_and_busy_stack_are_rejected(self):
        for changed in (
            "AccountId",
            "IAMRoleName",
            "DatadogSite",
            "busy",
            "unrecognized",
        ):
            with self.subTest(changed=changed):
                current = stack()
                if changed == "busy":
                    current["StackStatus"] = "UPDATE_IN_PROGRESS"
                elif changed == "unrecognized":
                    self.cf.describe_stack_resources.return_value = {
                        "StackResources": []
                    }
                else:
                    next(
                        param
                        for param in current["Parameters"]
                        if param["ParameterKey"] == changed
                    )["ParameterValue"] = "different"
                self.existing(**current)
                with self.assertRaises(launcher.InstallationError):
                    self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_customer_resources_are_rejected_before_planning(self):
        self.existing()
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": "CustomerWorkloadRole",
                "ResourceType": "AWS::IAM::Role",
            }
        )
        with self.assertRaisesRegex(launcher.InstallationError, "customized"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_expected_logical_id_with_wrong_type_is_rejected(self):
        self.existing()
        self.cf.describe_stack_resources.return_value["StackResources"][0][
            "ResourceType"
        ] = "AWS::IAM::Role"
        with self.assertRaisesRegex(launcher.InstallationError, "customized"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_deleted_resources_do_not_block_a_repaired_stack(self):
        self.existing()
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": "CustomerWorkloadRole",
                "ResourceType": "AWS::IAM::Role",
                "ResourceStatus": "DELETE_COMPLETE",
            }
        )
        self.subject.prepare(event())
        self.cf.create_change_set.assert_called_once()

    def test_ecs_only_ignores_orphaned_forwarding_resources(self):
        self.events.list_rules.return_value = {"Rules": [{"Name": "leftover-ec2-rule"}]}
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        self.subject.prepare(request)
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.assertEqual(self.events.mock_calls, [])

    def test_ecs_only_ignores_forwarding_health_and_primary_region(self):
        self.forwarding()
        self.iam.get_role.return_value["Role"]["Tags"] = [
            {"Key": "datadog:primary-region", "Value": "disabled-region"}
        ]
        self.events.describe_connection.return_value["ConnectionState"] = "DEAUTHORIZED"
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        result = self.subject.prepare(request)
        self.assertEqual(result["Region"], self.subject.home)
        self.assertEqual(
            self.values()["CreateResourceUpdateForwarding"]["ParameterValue"], "false"
        )
        self.assertEqual(self.events.mock_calls, [])

    def test_ecs_only_preserves_external_owner_checks(self):
        owner = FORWARDING.replace("DatadogAgentInstallation-", "RenamedInstallation-")
        self.forwarding(owner)
        self.cf.describe_stacks.side_effect = None
        self.cf.describe_stacks.return_value = {
            "Stacks": [stack(StackId=owner, RootId="renamed-root")]
        }
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        with (
            patch.object(self.subject, "find_stack", return_value=None),
            self.assertRaisesRegex(launcher.InstallationError, "Another installation"),
        ):
            self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()
        self.assertEqual(self.events.mock_calls, [])

    def test_ecs_only_accepts_quickstart_owner_without_checking_pipeline(self):
        owner = FORWARDING.replace("DatadogAgentInstallation-", "DatadogIntegration-")
        self.forwarding(owner)
        self.cf.describe_stacks.side_effect = None
        self.cf.describe_stacks.return_value = {
            "Stacks": [stack(StackId=owner, RootId="quickstart-root")]
        }
        self.cf.describe_stack_resources.return_value = {
            "StackResources": [{"LogicalResourceId": "DatadogIntegrationRoleStack"}]
        }
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        with patch.object(self.subject, "find_stack", return_value=None):
            self.subject.prepare(request)
        self.cf.describe_stack_resources.assert_any_call(StackName="quickstart-root")
        self.cf.describe_stack_resources.assert_any_call(
            PhysicalResourceId=launcher.FORWARDING_ROLE,
            LogicalResourceId="DDEventBridgeCrossRegionRole",
        )
        self.assertEqual(self.events.mock_calls, [])

    def test_saved_forwarded_types_still_require_healthy_forwarding_for_ecs_request(
        self,
    ):
        self.existing()
        self.forwarding()
        self.events.describe_connection.return_value["ConnectionState"] = "DEAUTHORIZED"
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        with self.assertRaisesRegex(launcher.InstallationError, "incomplete"):
            self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()

    def test_ecs_only_does_not_remove_preexisting_owned_forwarding(self):
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ecs:cluster"
        )
        self.existing(
            Parameters=[
                {"ParameterKey": key, "ParameterValue": value}
                for key, value in request["ResourceProperties"].items()
            ]
        )
        self.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        with self.assertRaisesRegex(
            launcher.InstallationError, "remove installation-owned"
        ):
            self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()

    def test_duplicate_delivery_resumes_original_baseline_without_replanning(self):
        self.existing(LastUpdatedTime="changed-after-planning")
        self.cf.describe_change_set.side_effect = None
        self.cf.describe_change_set.return_value = {
            "Description": json.dumps(
                {"Launcher": REQUEST, "Baseline": "original-baseline"}
            )
        }
        result = self.subject.prepare(event())
        self.assertEqual(result["Baseline"], "original-baseline")
        self.cf.create_change_set.assert_not_called()
        self.iam.get_role.assert_not_called()

    def test_duplicate_create_resumes_review_stack_without_parameters(self):
        self.existing(StackStatus="REVIEW_IN_PROGRESS", Parameters=[])
        self.cf.describe_change_set.side_effect = None
        self.cf.describe_change_set.return_value = {
            "Description": json.dumps({"Launcher": REQUEST, "Baseline": None})
        }
        result = self.subject.prepare(event())
        self.assertIsNone(result["Baseline"])
        self.cf.create_change_set.assert_not_called()

    def test_template_downgrade_rejected(self):
        self.existing(
            Tags=[
                {
                    "Key": "datadog:agent-installation-template-version",
                    "Value": "v4.24.0",
                }
            ]
        )
        with self.assertRaisesRegex(launcher.InstallationError, "older"):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_empty_and_unknown_resource_types_rejected(self):
        for value in ("", "aws:unknown:resource"):
            request = event()
            request["ResourceProperties"]["InstrumentationResourceTypes"] = value
            with self.assertRaises(launcher.InstallationError):
                self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()

    def test_external_forwarding_requires_matching_integration_identity(self):
        self.forwarding()
        for key in ("IAMRoleName", "DatadogSite", "ExternalId"):
            for value in (None, "another-integration"):
                with self.subTest(key=key, value=value):
                    self.owner_identity = {**PROPS, key: value}
                    with self.assertRaisesRegex(
                        launcher.InstallationError, "different or unverifiable"
                    ):
                        self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_untracked_external_forwarding_is_not_assumed_compatible(self):
        self.forwarding(owner=None)
        with self.assertRaisesRegex(
            launcher.InstallationError, "no verifiable integration owner"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_external_identity_read_denial_is_not_compatibility(self):
        self.forwarding()

        def describe(**kwargs):
            if kwargs["StackName"] == QUICKSTART_ROOT:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DescribeStacks",
                )
            return self.describe_stacks(**kwargs)

        self.cf.describe_stacks.side_effect = describe
        with self.assertRaises(ClientError):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_unknown_legacy_permissions_require_manual_review(self):
        self.existing()
        self.permissions_scope = "role"
        self.cf.get_template.return_value = {"TemplateBody": "unknown template"}
        with self.assertRaisesRegex(
            launcher.InstallationError, "not supported for automatic adoption"
        ):
            self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_existing_target_cannot_switch_org_or_adopt_unknown_identity(self):
        for value in (None, "another-org-external-id"):
            current = stack()
            for param in current["Parameters"]:
                if param["ParameterKey"] == "ExternalId":
                    param["ParameterValue"] = value
            self.existing(**current)
            with self.assertRaises(launcher.InstallationError):
                self.subject.prepare(event())
        self.cf.create_change_set.assert_not_called()

    def test_missing_requested_identity_blocks_creation(self):
        request = event()
        del request["ResourceProperties"]["ExternalId"]
        with self.assertRaisesRegex(
            launcher.InstallationError, "external ID is missing"
        ):
            self.subject.prepare(request)
        self.cf.create_change_set.assert_not_called()

    def test_pending_first_install_has_safe_recovery_instructions(self):
        self.existing(StackStatus="REVIEW_IN_PROGRESS", Parameters=[])
        with self.assertRaises(launcher.InstallationError) as caught:
            self.subject.prepare(event())
        self.assertEqual(caught.exception.stack_id, ROOT)
        self.assertIn(
            "If the original launcher is running, wait", str(caught.exception)
        )
        self.assertIn("no resources were deployed", str(caught.exception))
        self.cf.create_change_set.assert_not_called()
        self.cf.delete_stack.assert_not_called()

    def test_all_regions_checked_and_multiple_stacks_rejected(self):
        second = Mock()
        second.describe_stacks.return_value = {
            "Stacks": [stack(StackId=ROOT.replace("us-east-1", "eu-west-1"))]
        }
        self.subject.cloudformation["eu-west-1"] = second
        self.subject.regions.append("eu-west-1")
        found = self.subject.find_stack()
        self.assertEqual(found["StackId"].split(":")[3], "eu-west-1")
        self.existing()
        with self.assertRaisesRegex(launcher.InstallationError, "Multiple"):
            self.subject.find_stack()


class RoleOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.fixture = LauncherTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.forwarding()
        self.subject = self.fixture.subject
        self.role = self.fixture.iam.get_role.return_value["Role"]
        self.record = self.fixture.describe_stack_resources(
            PhysicalResourceId=launcher.FORWARDING_ROLE,
            LogicalResourceId="DDEventBridgeCrossRegionRole",
        )["StackResources"][0]

    def owner(self):
        return self.subject.role_owner(
            self.role, "DDEventBridgeCrossRegionRole", ACCOUNT
        )

    def response(self, records):
        original = self.fixture.describe_stack_resources

        def describe(**kwargs):
            if "PhysicalResourceId" in kwargs:
                return {"StackResources": records}
            return original(**kwargs)

        self.fixture.cf.describe_stack_resources.side_effect = describe

    def test_untagged_quickstart_uses_exact_resource_lookup(self):
        self.fixture.subject.prepare(event())
        self.fixture.cf.describe_stack_resources.assert_any_call(
            PhysicalResourceId=launcher.FORWARDING_ROLE,
            LogicalResourceId="DDEventBridgeCrossRegionRole",
        )
        self.assertEqual(
            self.fixture.values()["CreateResourceUpdateForwarding"]["ParameterValue"],
            "false",
        )
        self.fixture.iam.tag_role.assert_not_called()
        self.fixture.events.put_rule.assert_not_called()

    def test_tags_cannot_override_a_conflicting_resource_owner(self):
        self.fixture.existing()
        self.fixture.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        self.role["Tags"].append(
            {"Key": "aws:cloudformation:stack-id", "Value": FORWARDING}
        )
        with self.assertRaisesRegex(launcher.InstallationError, "ownership"):
            self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_resource_record_must_match_role_type_name_logical_id_and_account(self):
        for key, value in (
            ("ResourceType", "AWS::IAM::InstanceProfile"),
            ("PhysicalResourceId", "another-role"),
            ("LogicalResourceId", "AnotherRole"),
            ("StackId", QUICKSTART_FORWARDING.replace(ACCOUNT, "999999999999")),
            ("StackId", QUICKSTART_FORWARDING.replace("us-east-1", "us-west-2")),
            ("StackId", ""),
        ):
            with self.subTest(key=key, value=value):
                self.response([{**self.record, key: value}])
                with self.assertRaisesRegex(launcher.InstallationError, "ownership"):
                    self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_multiple_matching_resource_records_are_rejected(self):
        self.response([self.record, self.record])
        with self.assertRaisesRegex(launcher.InstallationError, "ambiguous"):
            self.owner()

    def test_unstable_resource_or_owner_is_rejected(self):
        for status in (
            "CREATE_IN_PROGRESS",
            "UPDATE_IN_PROGRESS",
            "DELETE_IN_PROGRESS",
            "DELETE_FAILED",
            "ROLLBACK_COMPLETE",
        ):
            with self.subTest(resource_status=status):
                self.response([{**self.record, "ResourceStatus": status}])
                with self.assertRaisesRegex(launcher.InstallationError, "not stable"):
                    self.owner()
            with self.subTest(stack_status=status):
                self.response([self.record])
                self.fixture.cf.describe_stacks.side_effect = None
                self.fixture.cf.describe_stacks.return_value = {
                    "Stacks": [stack(StackId=QUICKSTART_FORWARDING, StackStatus=status)]
                }
                with self.assertRaisesRegex(launcher.InstallationError, "not stable"):
                    self.owner()
                self.fixture.cf.describe_stacks.side_effect = (
                    self.fixture.describe_stacks
                )

    def test_deleted_resource_or_retained_resource_in_deleted_stack_is_not_an_owner(
        self,
    ):
        self.response([{**self.record, "ResourceStatus": "DELETE_COMPLETE"}])
        with self.assertRaisesRegex(launcher.InstallationError, "no verifiable"):
            self.owner()
        self.response([{**self.record, "ResourceStatus": "DELETE_SKIPPED"}])
        self.fixture.cf.describe_stacks.side_effect = None
        self.fixture.cf.describe_stacks.return_value = {
            "Stacks": [
                stack(StackId=QUICKSTART_FORWARDING, StackStatus="DELETE_COMPLETE")
            ]
        }
        with self.assertRaisesRegex(launcher.InstallationError, "no verifiable"):
            self.owner()

    def test_recreated_role_cannot_use_a_stale_resource_record(self):
        self.role["CreateDate"] = datetime(2026, 1, 3, tzinfo=UTC)
        with self.assertRaisesRegex(launcher.InstallationError, "recreated"):
            self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_matching_resource_must_resolve_to_the_same_stack(self):
        self.fixture.cf.describe_stacks.side_effect = None
        self.fixture.cf.describe_stacks.return_value = {"Stacks": [stack()]}
        with self.assertRaisesRegex(launcher.InstallationError, "does not match"):
            self.owner()

    def test_unstable_quickstart_root_blocks_preparation(self):
        def describe(**kwargs):
            result = self.fixture.describe_stacks(**kwargs)
            if kwargs["StackName"] == QUICKSTART_ROOT:
                result["Stacks"][0]["StackStatus"] = "UPDATE_IN_PROGRESS"
            return result

        self.fixture.cf.describe_stacks.side_effect = describe
        with self.assertRaisesRegex(launcher.InstallationError, "not stable"):
            self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_other_region_errors_are_not_treated_as_absence(self):
        other = Mock()
        self.subject.regions.append("us-west-2")
        self.subject.cloudformation["us-west-2"] = other
        for code, message in (
            ("AccessDenied", "denied"),
            ("Throttling", "slow down"),
            ("ValidationError", "invalid request"),
            ("ValidationError", "Stack for another-role does not exist"),
        ):
            with self.subTest(code=code, message=message):
                other.describe_stack_resources.side_effect = ClientError(
                    {"Error": {"Code": code, "Message": message}},
                    "DescribeStackResources",
                )
                with (
                    patch.object(self.subject, "find_stack", return_value=None),
                    self.assertRaises(ClientError),
                ):
                    self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_multiple_regions_claiming_same_role_are_rejected(self):
        other = Mock()
        owner = QUICKSTART_FORWARDING.replace("us-east-1", "us-west-2")
        other.describe_stack_resources.return_value = {
            "StackResources": [{**self.record, "StackId": owner}]
        }
        other.describe_stacks.return_value = {"Stacks": [stack(StackId=owner)]}
        self.subject.regions.append("us-west-2")
        self.subject.cloudformation["us-west-2"] = other
        with (
            patch.object(self.subject, "find_stack", return_value=None),
            self.assertRaisesRegex(launcher.InstallationError, "ambiguous"),
        ):
            self.subject.prepare(event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_sdk_reverse_lookup_ignores_only_exact_absence_and_deleted_history(self):
        session = launcher.boto3.Session(
            aws_access_key_id="test", aws_secret_access_key="test"
        )
        for old_state in ("absent", "deleted"):
            with self.subTest(old_state=old_state):
                home = session.client("cloudformation", region_name="us-east-1")
                remote = session.client("cloudformation", region_name="us-west-2")
                owner = QUICKSTART_FORWARDING.replace("us-east-1", "us-west-2")
                request = {
                    "PhysicalResourceId": launcher.FORWARDING_ROLE,
                    "LogicalResourceId": "DDEventBridgeCrossRegionRole",
                }
                self.subject.regions = ["us-east-1", "us-west-2"]
                self.subject.cloudformation = {"us-east-1": home, "us-west-2": remote}
                with Stubber(home) as home_stub, Stubber(remote) as remote_stub:
                    if old_state == "absent":
                        home_stub.add_client_error(
                            "describe_stack_resources",
                            "ValidationError",
                            f"Stack for {launcher.FORWARDING_ROLE} does not exist",
                            expected_params=request,
                        )
                    else:
                        home_stub.add_response(
                            "describe_stack_resources",
                            {"StackResources": [self.record]},
                            request,
                        )
                        home_stub.add_response(
                            "describe_stacks",
                            {
                                "Stacks": [
                                    {
                                        "StackId": QUICKSTART_FORWARDING,
                                        "StackName": "old",
                                        "CreationTime": ROLE_CREATED,
                                        "StackStatus": "DELETE_COMPLETE",
                                    }
                                ]
                            },
                            {"StackName": QUICKSTART_FORWARDING},
                        )
                    remote_stub.add_response(
                        "describe_stack_resources",
                        {"StackResources": [{**self.record, "StackId": owner}]},
                        request,
                    )
                    remote_stub.add_response(
                        "describe_stacks",
                        {
                            "Stacks": [
                                {
                                    "StackId": owner,
                                    "StackName": "current",
                                    "CreationTime": ROLE_CREATED,
                                    "StackStatus": "CREATE_COMPLETE",
                                }
                            ]
                        },
                        {"StackName": owner},
                    )
                    self.assertEqual(self.owner(), owner)
                    home_stub.assert_no_pending_responses()
                    remote_stub.assert_no_pending_responses()


class PermissionsCodeRefreshTest(unittest.TestCase):
    def setUp(self):
        self.child = ROOT.replace("/target", "-permissions/child")
        self.name = "permissions-function"
        self.arn = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:{self.name}"
        self.trigger = {
            "LogicalResourceId": launcher.PERMISSIONS_TRIGGER,
            "PhysicalResourceId": self.child + "/trigger",
            "ResourceType": "Custom::DatadogAttachIntegrationPermissionsFunctionTrigger",
            "Action": "Modify",
            "Replacement": "Conditional",
            "Scope": ["Properties"],
            "Details": [
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "InstrumentationRefreshToken",
                        "RequiresRecreation": "Conditionally",
                    },
                    "ChangeSource": "ParameterReference",
                    "Evaluation": "Static",
                    "CausingEntity": "InstrumentationRefreshToken",
                },
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "ServiceToken",
                        "RequiresRecreation": "Conditionally",
                    },
                    "ChangeSource": "ResourceAttribute",
                    "Evaluation": "Dynamic",
                    "CausingEntity": launcher.PERMISSIONS_FUNCTION + ".Arn",
                },
            ],
        }
        self.function = {
            "LogicalResourceId": launcher.PERMISSIONS_FUNCTION,
            "PhysicalResourceId": self.name,
            "ResourceType": "AWS::Lambda::Function",
            "Action": "Modify",
            "Replacement": "False",
            "Scope": ["Properties"],
            "Details": [
                {
                    "Target": {
                        "Attribute": "Properties",
                        "Name": "Code",
                        "RequiresRecreation": "Never",
                    },
                    "ChangeSource": "DirectModification",
                    "Evaluation": "Static",
                }
            ],
        }
        self.header = {
            "StackId": self.child,
            "Status": "CREATE_COMPLETE",
            "IncludeNestedStacks": True,
        }
        self.pages = [
            {**self.header, "Changes": [{"ResourceChange": r}]}
            for r in [self.trigger, self.function]
        ]
        self.resolved = json.loads(json.dumps(self.trigger))
        props = {
            "ServiceToken": self.arn,
            "IAMRoleName": "integration-role",
            "InstrumentationResourceTypes": "aws:ec2:instance",
            "InstrumentationRefreshToken": "before",
        }
        self.resolved["BeforeContext"] = json.dumps({"Properties": props})
        self.resolved["AfterContext"] = json.dumps(
            {"Properties": {**props, "InstrumentationRefreshToken": "after"}}
        )
        self.resources = [
            {
                "LogicalResourceId": r["LogicalResourceId"],
                "PhysicalResourceId": r["PhysicalResourceId"],
                "ResourceType": r["ResourceType"],
                "ResourceStatus": "CREATE_COMPLETE",
            }
            for r in [self.trigger, self.function]
        ]
        self.child_state = {
            "StackId": self.child,
            "StackStatus": "UPDATE_COMPLETE",
            "ParentId": ROOT,
            "RootId": ROOT,
            "Parameters": [
                {
                    "ParameterKey": "InstrumentationResourceTypes",
                    "ParameterValue": "aws:ec2:instance",
                }
            ],
        }
        self.root_resource = {
            "LogicalResourceId": launcher.OWNED_PERMISSIONS_RESOURCE,
            "PhysicalResourceId": self.child,
            "ResourceType": "AWS::CloudFormation::Stack",
            "Action": "Modify",
            "Replacement": "False",
            "ChangeSetId": NESTED_CHANGE_SET,
        }
        self.root_page = {
            "StackId": ROOT,
            "Status": "CREATE_COMPLETE",
            "IncludeNestedStacks": True,
            "Changes": [{"ResourceChange": self.root_resource}],
        }
        self.client = Mock()
        self.client.describe_stacks.side_effect = lambda **kwargs: {
            "Stacks": [self.child_state]
        }
        self.client.describe_stack_resources.side_effect = lambda **kwargs: {
            "StackResources": [
                {**r, "StackId": self.child}
                for r in self.resources
                if r["LogicalResourceId"] == kwargs["LogicalResourceId"]
            ]
        }
        self.client.get_paginator.side_effect = lambda name: SimpleNamespace(
            paginate=lambda **kwargs: self.paginate(name, kwargs)
        )
        self.resolved_reads = 0

    def paginate(self, name, kwargs):
        if name != "describe_change_set":
            raise AssertionError(
                "The guard must reuse the existing DescribeStackResources permission"
            )
        if kwargs["ChangeSetName"] == CHANGE_SET:
            return [self.root_page]
        if kwargs.get("IncludePropertyValues"):
            self.resolved_reads += 1
            return [{**self.header, "Changes": [{"ResourceChange": self.resolved}]}]
        return self.pages

    def set_after(self, **updates):
        value = json.loads(self.resolved["AfterContext"])
        value["Properties"].update(updates)
        self.resolved["AfterContext"] = json.dumps(value)

    def check(self, **kwargs):
        launcher.assert_safe_changes(
            self.client, CHANGE_SET, preserve_legacy=True, **kwargs
        )

    def test_same_function_code_update_with_resolved_unchanged_arn(self):
        self.check()
        self.assertEqual(self.resolved_reads, 1)
        self.client.describe_stacks.assert_called_once_with(StackName=self.child)

    def test_identity_reads_use_existing_permission_and_exact_logical_ids(self):
        self.check()
        self.assertEqual(
            [
                lookup.kwargs
                for lookup in self.client.describe_stack_resources.call_args_list
            ],
            [
                {
                    "StackName": self.child,
                    "LogicalResourceId": launcher.PERMISSIONS_TRIGGER,
                },
                {
                    "StackName": self.child,
                    "LogicalResourceId": launcher.PERMISSIONS_FUNCTION,
                },
            ],
        )
        self.client.list_stack_resources.assert_not_called()

    def test_code_update_does_not_allow_frozen_child_changes(self):
        self.root_resource["LogicalResourceId"] = launcher.PERMISSIONS_RESOURCE
        with self.assertRaises(launcher.InstallationError):
            self.check()
        self.client.describe_stacks.assert_not_called()

    def test_code_update_does_not_allow_indirect_permissions_child(self):
        with self.assertRaises(launcher.InstallationError):
            launcher.assert_safe_changes(self.client, NESTED_CHANGE_SET, is_root=False)
        self.client.describe_stacks.assert_not_called()

    def test_unsafe_code_or_identity_changes_are_rejected(self):
        mutations = {
            "function replacement": lambda: self.function.update(Replacement="True"),
            "conditional function replacement": lambda: self.function.update(
                Replacement="Conditional"
            ),
            "different function": lambda: self.function.update(
                PhysicalResourceId="another-function"
            ),
            "different trigger": lambda: self.trigger.update(
                PhysicalResourceId="another-trigger"
            ),
            "runtime change": lambda: self.function["Details"][0]["Target"].update(
                Name="Runtime"
            ),
            "unknown service token reference": lambda: self.trigger["Details"][
                1
            ].update(CausingEntity="OtherFunction.Arn"),
            "direct service token change": lambda: self.trigger["Details"][1].update(
                ChangeSource="DirectModification"
            ),
            "extra resource change": lambda: self.pages[1]["Changes"].append(
                {"ResourceChange": {"LogicalResourceId": "AnotherResource"}}
            ),
            "wrong page stack": lambda: self.pages[1].update(StackId=ROOT),
            "busy child": lambda: self.child_state.update(
                StackStatus="UPDATE_IN_PROGRESS"
            ),
            "indirect child": lambda: self.child_state.update(ParentId="other-parent"),
            "busy function": lambda: self.resources[1].update(
                ResourceStatus="UPDATE_IN_PROGRESS"
            ),
            "different resolved trigger": lambda: self.resolved.update(
                PhysicalResourceId="different"
            ),
            "wrong resolved page": lambda: self.header.update(StackId=ROOT),
            "missing live function": lambda: self.resources.pop(1),
            "same foreign ARN": lambda: self.resolved.update(
                BeforeContext=self.resolved["BeforeContext"].replace(
                    self.arn, self.arn + ":alias"
                ),
                AfterContext=self.resolved["AfterContext"].replace(
                    self.arn, self.arn + ":alias"
                ),
            ),
            "changed ARN": lambda: self.set_after(ServiceToken=self.arn + ":alias"),
            "changed role": lambda: self.set_after(IAMRoleName="different-role"),
            "unverified type change": lambda: self.set_after(
                InstrumentationResourceTypes="aws:lambda:function"
            ),
            "missing context": lambda: self.resolved.pop("AfterContext"),
            "malformed context": lambda: self.resolved.update(AfterContext="not-json"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                with self.assertRaises(launcher.InstallationError):
                    mutate()
                    self.check()

    def test_code_update_retains_verified_type_union_rules(self):
        previous = {"aws:ec2:instance"}
        expanded = previous | {"aws:lambda:function"}
        value = ",".join(sorted(expanded))
        self.set_after(InstrumentationResourceTypes=value)
        self.trigger["Details"].append(
            {
                "Target": {
                    "Attribute": "Properties",
                    "Name": "InstrumentationResourceTypes",
                    "RequiresRecreation": "Conditionally",
                },
                "ChangeSource": "ParameterReference",
                "Evaluation": "Static",
                "CausingEntity": "InstrumentationResourceTypes",
            }
        )
        for page in self.pages:
            page["Parameters"] = [
                {
                    "ParameterKey": "InstrumentationResourceTypes",
                    "ParameterValue": value,
                }
            ]
        self.check(type_expansion=(previous, expanded))
        with self.assertRaises(launcher.InstallationError):
            self.check(type_expansion=(expanded, previous))


class PollTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.describe_stacks.return_value = {"Stacks": [stack()]}
        self.client.describe_change_set.return_value = {
            "Status": "CREATE_COMPLETE",
            "ExecutionStatus": "AVAILABLE",
            "ChangeSetId": CHANGE_SET,
            "IncludeNestedStacks": True,
            "Changes": [],
        }
        self.pages = {CHANGE_SET: [self.client.describe_change_set.return_value]}
        self.change_paginator = Mock()
        self.change_paginator.paginate.side_effect = lambda **kwargs: self.pages[
            kwargs["ChangeSetName"]
        ]
        self.client.get_paginator.side_effect = lambda operation: (
            self.change_paginator
            if operation == "describe_change_set"
            else self.client.get_paginator.return_value
        )

    def nested_changes(self, changes):
        self.pages[CHANGE_SET][0]["Changes"] = [
            {
                "ResourceChange": {
                    "Action": "Modify",
                    "LogicalResourceId": launcher.PERMISSIONS_RESOURCE,
                    "ResourceType": "AWS::CloudFormation::Stack",
                    "Replacement": "False",
                    "ChangeSetId": NESTED_CHANGE_SET,
                    "BeforeContext": json.dumps(
                        {
                            "Properties": {
                                "TemplateURL": "https://test-bucket.s3.amazonaws.com/aws/v4.21.1/datadog_integration_permissions.yaml"
                            }
                        }
                    ),
                    "AfterContext": json.dumps(
                        {
                            "Properties": {
                                "TemplateURL": "https://test-bucket.s3.amazonaws.com/aws/v4.21.1/datadog_integration_permissions.yaml"
                            }
                        }
                    ),
                }
            }
        ]
        self.pages[NESTED_CHANGE_SET] = [
            {
                "Status": "CREATE_COMPLETE",
                "ExecutionStatus": "UNAVAILABLE",
                "IncludeNestedStacks": True,
                "Changes": changes,
            }
        ]

    def test_executes_once_with_token_and_rollback_enabled(self):
        current = state()
        self.assertFalse(launcher.poll_change_set(self.client, event(), current))
        self.client.execute_change_set.assert_called_once_with(
            StackName=ROOT,
            ChangeSetName=CHANGE_SET,
            ClientRequestToken=launcher.request_name(event()),
            DisableRollback=False,
        )
        self.assertEqual(current["Phase"], "deployment")

    def test_new_stack_change_set_can_execute(self):
        self.client.describe_stacks.return_value = {
            "Stacks": [stack(StackStatus="REVIEW_IN_PROGRESS")]
        }
        self.assertFalse(
            launcher.poll_change_set(self.client, event(), state(baseline=None))
        )
        self.client.execute_change_set.assert_called_once()

    def test_concurrent_update_between_read_and_change_set_does_not_overwrite_types(
        self,
    ):
        self.client.describe_stacks.return_value = {
            "Stacks": [stack(LastUpdatedTime="newer")]
        }
        with self.assertRaisesRegex(launcher.InstallationError, "Another request"):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_executed_request_is_not_executed_again(self):
        self.client.describe_change_set.return_value["ExecutionStatus"] = (
            "EXECUTE_IN_PROGRESS"
        )
        current = state()
        self.assertFalse(launcher.poll_change_set(self.client, event(), current))
        self.assertEqual(current["Phase"], "deployment")
        self.client.execute_change_set.assert_not_called()

    def test_noop_is_success_but_other_change_set_failures_are_not(self):
        self.client.describe_change_set.return_value = {
            "Status": "FAILED",
            "ExecutionStatus": "UNAVAILABLE",
            "StatusReason": "The submitted information didn't contain changes.",
        }
        self.assertTrue(launcher.poll_change_set(self.client, event(), state()))
        self.client.describe_change_set.return_value["StatusReason"] = (
            "Invalid template"
        )
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_destructive_or_unrecognized_root_changes_are_not_executed(self):
        for action, replacement, logical_id in (
            ("Remove", "False", launcher.PERMISSIONS_RESOURCE),
            ("Modify", "True", launcher.PERMISSIONS_RESOURCE),
            ("Modify", "Conditional", launcher.PERMISSIONS_RESOURCE),
            ("Modify", None, launcher.PERMISSIONS_RESOURCE),
            ("Add", "False", "CustomerWorkloadRole"),
        ):
            with self.subTest(
                action=action, replacement=replacement, logical_id=logical_id
            ):
                self.pages[CHANGE_SET][0]["Changes"] = [
                    {
                        "ResourceChange": {
                            "Action": action,
                            "Replacement": replacement,
                            "LogicalResourceId": logical_id,
                            "ResourceType": "AWS::CloudFormation::Stack",
                        }
                    }
                ]
                with self.assertRaisesRegex(
                    launcher.InstallationError, "Review it manually"
                ):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_destructive_nested_changes_are_not_executed(self):
        for action, replacement in (
            ("Remove", "False"),
            ("Modify", "True"),
            ("Modify", "Conditional"),
        ):
            with self.subTest(action=action, replacement=replacement):
                self.nested_changes(
                    [
                        {
                            "ResourceChange": {
                                "Action": action,
                                "Replacement": replacement,
                                "LogicalResourceId": "CustomerWorkloadRole",
                                "ResourceType": "AWS::IAM::Role",
                            }
                        }
                    ]
                )
                with self.assertRaisesRegex(
                    launcher.InstallationError, "Review it manually"
                ):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def permissions_refresh_change(self):
        return {
            "ResourceChange": {
                "Action": "Modify",
                "Replacement": "Conditional",
                "LogicalResourceId": "DatadogAttachIntegrationPermissionsFunctionTrigger",
                "ResourceType": "Custom::DatadogAttachIntegrationPermissionsFunctionTrigger",
                "Scope": ["Properties"],
                "Details": [
                    {
                        "ChangeSource": source,
                        "Evaluation": evaluation,
                        **({"CausingEntity": cause} if cause else {}),
                        "Target": {
                            "Attribute": "Properties",
                            "Name": "InstrumentationRefreshToken",
                            "RequiresRecreation": "Conditionally",
                        },
                    }
                    for source, evaluation, cause in (
                        ("ParameterReference", "Static", "InstrumentationRefreshToken"),
                        ("DirectModification", "Dynamic", None),
                    )
                ],
            }
        }

    def test_refresh_only_permissions_update_executes_inspected_root(self):
        for logical_id in (
            launcher.PERMISSIONS_RESOURCE,
            launcher.OWNED_PERMISSIONS_RESOURCE,
        ):
            with self.subTest(logical_id=logical_id):
                self.client.execute_change_set.reset_mock()
                self.nested_changes([self.permissions_refresh_change()])
                self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"][
                    "LogicalResourceId"
                ] = logical_id
                launcher.poll_change_set(self.client, event(), state())
                self.client.execute_change_set.assert_called_once_with(
                    StackName=ROOT,
                    ChangeSetName=CHANGE_SET,
                    ClientRequestToken=launcher.request_name(event()),
                    DisableRollback=False,
                )

    def configure_type_expansion(self, logical_id=launcher.PERMISSIONS_RESOURCE):
        self.client.execute_change_set.reset_mock()
        change = self.permissions_refresh_change()
        for detail in list(change["ResourceChange"]["Details"]):
            added = json.loads(json.dumps(detail))
            added["Target"]["Name"] = "InstrumentationResourceTypes"
            if added.get("CausingEntity"):
                added["CausingEntity"] = "InstrumentationResourceTypes"
            change["ResourceChange"]["Details"].append(added)
        self.nested_changes([change])
        child_id = ROOT.replace("/target", "-permissions/child")
        root_change = self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"]
        root_change.update(LogicalResourceId=logical_id, PhysicalResourceId=child_id)
        proposed = [
            {
                "ParameterKey": "InstrumentationResourceTypes",
                "ParameterValue": "aws:ec2:instance,aws:lambda:function",
            }
        ]
        self.pages[CHANGE_SET][0]["Parameters"] = proposed
        self.pages[NESTED_CHANGE_SET][0].update(StackId=child_id, Parameters=proposed)
        child = stack(StackId=child_id)
        self.client.describe_stacks.side_effect = lambda **kwargs: {
            "Stacks": [stack() if kwargs["StackName"] == ROOT else child]
        }
        request = event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:lambda:function"
        )
        return request, child, change["ResourceChange"]

    def test_additive_permissions_update_executes_only_inspected_root(self):
        for logical_id in (
            launcher.PERMISSIONS_RESOURCE,
            launcher.OWNED_PERMISSIONS_RESOURCE,
        ):
            with self.subTest(logical_id=logical_id):
                request, child, _ = self.configure_type_expansion(logical_id)
                launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_called_once_with(
                    StackName=ROOT,
                    ChangeSetName=CHANGE_SET,
                    ClientRequestToken=launcher.request_name(request),
                    DisableRollback=False,
                )
                self.client.describe_stacks.assert_any_call(StackName=child["StackId"])

    def test_type_expansion_rejects_removal_and_unrequested_types(self):
        for proposed in (
            "aws:lambda:function",
            "aws:ec2:instance",
            "aws:ec2:instance,aws:lambda:function,aws:eks:cluster",
            "aws:ec2:instance,unknown",
            "",
        ):
            with self.subTest(proposed=proposed):
                request, _, _ = self.configure_type_expansion()
                self.pages[CHANGE_SET][0]["Parameters"] = [
                    {
                        "ParameterKey": "InstrumentationResourceTypes",
                        "ParameterValue": proposed,
                    }
                ]
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_requires_complete_matching_child_preview(self):
        for override in (
            {"StackId": "another-child"},
            {"Parameters": []},
            {
                "Parameters": [
                    {
                        "ParameterKey": "InstrumentationResourceTypes",
                        "ParameterValue": "aws:lambda:function",
                    }
                ]
            },
        ):
            with self.subTest(override=override):
                request, _, _ = self.configure_type_expansion()
                self.pages[NESTED_CHANGE_SET][0].update(override)
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_requires_stable_matching_existing_child(self):
        for override in (
            {"StackId": "another-child"},
            {"StackStatus": "UPDATE_IN_PROGRESS"},
            {"Parameters": []},
            {
                "Parameters": [
                    {
                        "ParameterKey": "InstrumentationResourceTypes",
                        "ParameterValue": "aws:eks:cluster",
                    }
                ]
            },
        ):
            with self.subTest(override=override):
                request, child, _ = self.configure_type_expansion()
                child.update(override)
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_rejects_other_properties_and_actual_replacement(self):
        for override in (
            {"Replacement": "True"},
            {"LogicalResourceId": "OtherTrigger"},
            {"Scope": ["Properties", "Metadata"]},
        ):
            with self.subTest(override=override):
                request, _, resource = self.configure_type_expansion()
                resource.update(override)
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()
        for name in (
            "ServiceToken",
            "DatadogIntegrationRole",
            "InstrumentationPolicyScope",
        ):
            with self.subTest(name=name):
                request, _, resource = self.configure_type_expansion()
                detail = json.loads(json.dumps(resource["Details"][0]))
                detail["Target"]["Name"] = name
                detail["CausingEntity"] = name
                resource["Details"].append(detail)
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_rejects_wrong_property_reference(self):
        request, _, resource = self.configure_type_expansion()
        resource["Details"][2]["CausingEntity"] = "InstrumentationRefreshToken"
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, request, state())
        self.client.execute_change_set.assert_not_called()

    def test_type_expansion_cannot_modify_frozen_or_forwarding_children(self):
        for logical_id, legacy in (
            (launcher.PERMISSIONS_RESOURCE, True),
            ("DatadogAgentResourceUpdateForwardingStack", False),
        ):
            with self.subTest(logical_id=logical_id):
                request, _, _ = self.configure_type_expansion(logical_id)
                current = {**state(), "PreserveLegacyPermissions": legacy}
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, current)
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_is_limited_to_the_direct_permissions_child(self):
        for at_root in (True, False):
            with self.subTest(at_root=at_root):
                request, _, resource = self.configure_type_expansion()
                if at_root:
                    self.pages[CHANGE_SET][0]["Changes"] = [
                        {"ResourceChange": resource}
                    ]
                else:
                    deeper_id = NESTED_CHANGE_SET + "-deeper"
                    child_page = self.pages[NESTED_CHANGE_SET][0]
                    self.pages[deeper_id] = [dict(child_page)]
                    child_page["Changes"] = [
                        {
                            "ResourceChange": {
                                "Action": "Modify",
                                "LogicalResourceId": launcher.PERMISSIONS_RESOURCE,
                                "ResourceType": "AWS::CloudFormation::Stack",
                                "Replacement": "False",
                                "ChangeSetId": deeper_id,
                            }
                        }
                    ]
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, request, state())
                self.client.execute_change_set.assert_not_called()

    def test_type_expansion_cannot_hide_later_page_replacement(self):
        request, _, resource = self.configure_type_expansion()
        page = self.pages[NESTED_CHANGE_SET][0]
        unsafe = json.loads(json.dumps(resource))
        unsafe["Replacement"] = "True"
        self.pages[NESTED_CHANGE_SET].append(
            {**page, "Changes": [{"ResourceChange": unsafe}]}
        )
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, request, state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_rejects_other_resources_and_replacements(self):
        for override in (
            {"LogicalResourceId": "OtherTrigger"},
            {"ResourceType": "Custom::OtherTrigger"},
            {"Action": "Remove"},
            {"Replacement": "True"},
            {"Replacement": None},
            {"Scope": ["Properties", "Metadata"]},
            {"Scope": []},
            {"Details": []},
            {"Details": None},
        ):
            with self.subTest(override=override):
                change = self.permissions_refresh_change()
                change["ResourceChange"].update(override)
                self.nested_changes([change])
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_rejects_other_property_changes(self):
        for attribute, name, recreation in (
            ("Properties", "ServiceToken", "Conditionally"),
            ("Properties", "DatadogIntegrationRole", "Conditionally"),
            ("Properties", "InstrumentationResourceTypes", "Conditionally"),
            ("Properties", "InstrumentationPolicyScope", "Conditionally"),
            ("Metadata", "InstrumentationRefreshToken", "Conditionally"),
            ("Properties", "InstrumentationRefreshToken", "Always"),
            ("Properties", "InstrumentationRefreshToken", None),
        ):
            with self.subTest(attribute=attribute, name=name, recreation=recreation):
                change = self.permissions_refresh_change()
                change["ResourceChange"]["Details"].append(
                    {
                        "ChangeSource": "DirectModification",
                        "Evaluation": "Dynamic",
                        "Target": {
                            "Attribute": attribute,
                            "Name": name,
                            "RequiresRecreation": recreation,
                        },
                    }
                )
                self.nested_changes([change])
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_rejects_unrecognized_change_sources(self):
        for override in (
            {"ChangeSource": "ResourceReference"},
            {"CausingEntity": "IAMRoleName"},
            {"Evaluation": "Dynamic"},
            {"Target": {}},
        ):
            with self.subTest(override=override):
                change = self.permissions_refresh_change()
                change["ResourceChange"]["Details"][0].update(override)
                self.nested_changes([change])
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_is_limited_to_direct_permissions_children(self):
        for parent in (launcher.FORWARDING_RESOURCE, launcher.SUPPLEMENTARY_RESOURCE):
            with self.subTest(parent=parent):
                self.nested_changes([self.permissions_refresh_change()])
                self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"][
                    "LogicalResourceId"
                ] = parent
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_cannot_apply_at_root(self):
        self.pages[CHANGE_SET][0]["Changes"] = [self.permissions_refresh_change()]
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_cannot_apply_below_permissions_child(self):
        deeper_change_set = NESTED_CHANGE_SET + "-deeper"
        self.nested_changes(
            [
                {
                    "ResourceChange": {
                        "Action": "Modify",
                        "LogicalResourceId": "UnexpectedNestedStack",
                        "ResourceType": "AWS::CloudFormation::Stack",
                        "Replacement": "False",
                        "ChangeSetId": deeper_change_set,
                    }
                }
            ]
        )
        self.pages[deeper_change_set] = [
            {
                "Status": "CREATE_COMPLETE",
                "IncludeNestedStacks": True,
                "Changes": [self.permissions_refresh_change()],
            }
        ]
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_cannot_modify_frozen_legacy_child(self):
        self.nested_changes([self.permissions_refresh_change()])
        with self.assertRaisesRegex(launcher.InstallationError, "frozen legacy"):
            launcher.assert_safe_changes(self.client, CHANGE_SET, preserve_legacy=True)
        self.client.execute_change_set.assert_not_called()

    def test_refresh_exception_does_not_hide_later_page_replacements(self):
        self.nested_changes([self.permissions_refresh_change()])
        unsafe = self.permissions_refresh_change()
        unsafe["ResourceChange"]["Replacement"] = "True"
        self.pages[NESTED_CHANGE_SET].append(
            {
                "Status": "CREATE_COMPLETE",
                "IncludeNestedStacks": True,
                "Changes": [unsafe],
            }
        )
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_safe_nested_changes_execute_only_the_root_change_set(self):
        self.nested_changes(
            [
                {
                    "ResourceChange": {
                        "Action": "Modify",
                        "Replacement": "False",
                        "LogicalResourceId": "PermissionsTrigger",
                        "ResourceType": "Custom::DatadogAttachIntegrationPermissions",
                    }
                }
            ]
        )
        launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_called_once()
        self.assertEqual(
            self.client.execute_change_set.call_args.kwargs["ChangeSetName"], CHANGE_SET
        )

    def test_new_nested_resources_are_allowed(self):
        self.nested_changes(
            [
                {
                    "ResourceChange": {
                        "Action": "Add",
                        "LogicalResourceId": "PermissionsRole",
                        "ResourceType": "AWS::IAM::Role",
                    }
                }
            ]
        )
        root_change = self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"]
        root_change["Action"] = "Add"
        root_change.pop("Replacement")
        self.client.describe_stacks.return_value = {
            "Stacks": [stack(StackStatus="REVIEW_IN_PROGRESS")]
        }
        launcher.poll_change_set(self.client, event(), state(baseline=None))
        self.client.execute_change_set.assert_called_once()

    def test_supplementary_nested_additions_use_the_same_approved_change_set(self):
        self.nested_changes(
            [
                {
                    "ResourceChange": {
                        "Action": "Add",
                        "LogicalResourceId": "DDEventForwardingRuleEKS",
                        "ResourceType": "AWS::Events::Rule",
                    }
                }
            ]
        )
        root = self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"]
        root.update(Action="Add", LogicalResourceId=launcher.SUPPLEMENTARY_RESOURCE)
        launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_called_once_with(
            StackName=ROOT,
            ChangeSetName=CHANGE_SET,
            ClientRequestToken=launcher.request_name(event()),
            DisableRollback=False,
        )

    def test_supplementary_removal_or_replacement_is_not_executed(self):
        for action, replacement in (("Remove", "False"), ("Modify", "True")):
            with self.subTest(action=action):
                self.nested_changes([])
                root = self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"]
                root.update(
                    Action=action,
                    Replacement=replacement,
                    LogicalResourceId=launcher.SUPPLEMENTARY_RESOURCE,
                )
                with self.assertRaisesRegex(
                    launcher.InstallationError, "Review it manually"
                ):
                    launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_later_nested_pages_are_also_checked(self):
        self.nested_changes([])
        self.pages[NESTED_CHANGE_SET].append(
            {
                "Status": "CREATE_COMPLETE",
                "IncludeNestedStacks": True,
                "Changes": [
                    {
                        "ResourceChange": {
                            "Action": "Remove",
                            "LogicalResourceId": "CustomerWorkloadRole",
                            "ResourceType": "AWS::IAM::Role",
                        }
                    }
                ],
            }
        )
        with self.assertRaisesRegex(launcher.InstallationError, "Review it manually"):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_failed_nested_preview_is_not_a_noop(self):
        self.nested_changes([])
        self.pages[NESTED_CHANGE_SET][0].update(
            Status="FAILED", StatusReason="Invalid template"
        )
        with self.assertRaisesRegex(
            launcher.InstallationError, "hierarchy is incomplete"
        ):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_nested_noop_does_not_block_root_update(self):
        self.nested_changes([])
        self.pages[NESTED_CHANGE_SET][0].update(
            Status="FAILED",
            StatusReason="The submitted information didn't contain changes.",
        )
        launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_called_once()

    def test_missing_nested_preview_blocks_execution(self):
        self.nested_changes([])
        del self.pages[CHANGE_SET][0]["Changes"][0]["ResourceChange"]["ChangeSetId"]
        with self.assertRaisesRegex(
            launcher.InstallationError, "nested installation change set is missing"
        ):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_incomplete_hierarchy_blocks_execution(self):
        self.pages[CHANGE_SET][0]["IncludeNestedStacks"] = False
        with self.assertRaisesRegex(
            launcher.InstallationError, "hierarchy is incomplete"
        ):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_nested_read_denial_is_not_a_safe_change_set(self):
        self.nested_changes([])

        def pages(**kwargs):
            if kwargs["ChangeSetName"] == NESTED_CHANGE_SET:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DescribeChangeSet",
                )
            return self.pages[kwargs["ChangeSetName"]]

        self.change_paginator.paginate.side_effect = pages
        with self.assertRaises(ClientError):
            launcher.poll_change_set(self.client, event(), state())
        self.client.execute_change_set.assert_not_called()

    def test_sdk_pagination_checks_changes_beyond_first_page(self):
        client = launcher.boto3.client(
            "cloudformation",
            region_name="us-east-1",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
        )
        first_page = {**self.pages[CHANGE_SET][0], "NextToken": "next-page"}
        second_page = {
            **self.pages[CHANGE_SET][0],
            "Changes": [
                {
                    "ResourceChange": {
                        "Action": "Remove",
                        "LogicalResourceId": "CustomerWorkloadRole",
                        "ResourceType": "AWS::IAM::Role",
                    }
                }
            ],
        }
        current_stack = stack(
            StackName=launcher.STACK_NAME, CreationTime=datetime(2026, 1, 1, tzinfo=UTC)
        )
        with Stubber(client) as stub:
            stub.add_response(
                "describe_change_set",
                first_page,
                {
                    "StackName": ROOT,
                    "ChangeSetName": launcher.request_name(event()),
                },
            )
            stub.add_response(
                "describe_stacks", {"Stacks": [current_stack]}, {"StackName": ROOT}
            )
            stub.add_response(
                "describe_change_set", first_page, {"ChangeSetName": CHANGE_SET}
            )
            stub.add_response(
                "describe_change_set",
                second_page,
                {
                    "ChangeSetName": CHANGE_SET,
                    "NextToken": "next-page",
                },
            )
            with self.assertRaisesRegex(
                launcher.InstallationError, "Review it manually"
            ):
                launcher.poll_change_set(
                    client, event(), state(baseline=launcher.fingerprint(current_stack))
                )
            stub.assert_no_pending_responses()

    def test_old_stack_success_does_not_complete_current_request(self):
        self.client.get_paginator.return_value.paginate.return_value = [
            {
                "StackEvents": [
                    {
                        "PhysicalResourceId": ROOT,
                        "ClientRequestToken": "older-request",
                        "ResourceStatus": "CREATE_COMPLETE",
                    }
                ]
            }
        ]
        self.assertFalse(
            launcher.poll_deployment(self.client, event(), state("deployment"))
        )

    def test_poll_handles_external_cancellation_without_mixing_operations(self):
        own_token = launcher.request_name(event())
        cases = [
            (
                "canceled rollback failed",
                [
                    (ROOT, "cancel", "UPDATE_ROLLBACK_FAILED"),
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                ],
                "failed",
            ),
            (
                "canceled rollback complete",
                [
                    (ROOT, "cancel", "UPDATE_ROLLBACK_COMPLETE"),
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                ],
                "failed",
            ),
            (
                "rollback still running",
                [
                    (ROOT, "cancel", "UPDATE_ROLLBACK_IN_PROGRESS"),
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                ],
                False,
            ),
            (
                "later unrelated failure",
                [
                    (ROOT, "other", "UPDATE_ROLLBACK_FAILED"),
                    (ROOT, own_token, "UPDATE_COMPLETE"),
                ],
                True,
            ),
            (
                "older failure",
                [
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                    (ROOT, "old", "UPDATE_ROLLBACK_FAILED"),
                ],
                False,
            ),
            (
                "request not visible yet",
                [(ROOT, "old", "UPDATE_ROLLBACK_FAILED")],
                False,
            ),
            (
                "nested failure",
                [
                    ("nested", "cancel", "UPDATE_ROLLBACK_FAILED"),
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                ],
                False,
            ),
            (
                "unrelated success",
                [
                    (ROOT, "other", "UPDATE_COMPLETE"),
                    (ROOT, own_token, "UPDATE_IN_PROGRESS"),
                ],
                False,
            ),
        ]
        for name, entries, expected in cases:
            with self.subTest(name=name):
                self.client.get_paginator.return_value.paginate.return_value = [
                    {
                        "StackEvents": [
                            {
                                "PhysicalResourceId": resource,
                                "ClientRequestToken": token,
                                "ResourceStatus": status,
                            }
                        ]
                    }
                    for resource, token, status in entries
                ]
                if expected == "failed":
                    with self.assertRaises(launcher.InstallationError):
                        launcher.poll_deployment(
                            self.client, event(), state("deployment")
                        )
                else:
                    self.assertEqual(
                        launcher.poll_deployment(
                            self.client, event(), state("deployment")
                        ),
                        expected,
                    )

    def test_poll_matches_request_root_event_not_nested_resources(self):
        entries = [
            {
                "PhysicalResourceId": "nested",
                "ClientRequestToken": launcher.request_name(event()),
                "ResourceStatus": "CREATE_COMPLETE",
            },
            {
                "PhysicalResourceId": ROOT,
                "ClientRequestToken": launcher.request_name(event()),
                "ResourceStatus": "UPDATE_IN_PROGRESS",
            },
        ]
        self.client.get_paginator.return_value.paginate.return_value = [
            {"StackEvents": entries}
        ]
        self.assertFalse(
            launcher.poll_deployment(self.client, event(), state("deployment"))
        )
        entries[1]["ResourceStatus"] = "UPDATE_COMPLETE"
        self.assertTrue(
            launcher.poll_deployment(self.client, event(), state("deployment"))
        )
        entries[1]["ResourceStatus"] = "UPDATE_ROLLBACK_COMPLETE"
        with self.assertRaises(launcher.InstallationError):
            launcher.poll_deployment(self.client, event(), state("deployment"))

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "time", return_value=10)
    def test_long_operation_continues_without_premature_success(
        self, now, client_factory, respond
    ):
        self.client.describe_change_set.return_value = {
            "Status": "CREATE_IN_PROGRESS",
            "ExecutionStatus": "UNAVAILABLE",
        }
        lambda_client = Mock()
        client_factory.side_effect = [self.client, lambda_client]
        context = Mock(invoked_function_arn="launcher-function")
        context.get_remaining_time_in_millis.return_value = 80_000
        launcher.wait_for_result(event(), context, state())
        args = lambda_client.invoke.call_args.kwargs
        self.assertEqual(args["InvocationType"], "Event")
        payload = json.loads(args["Payload"])
        self.assertEqual(payload["LauncherState"]["StackId"], ROOT)
        self.assertEqual(
            payload["ResourceProperties"],
            {
                "AccountId": ACCOUNT,
                "InstrumentationResourceTypes": PROPS["InstrumentationResourceTypes"],
            },
        )
        self.assertNotIn("test-api-secret", args["Payload"].decode())
        self.assertNotIn("test-app-secret", args["Payload"].decode())
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "time", return_value=10)
    def test_pending_update_resumes_with_requested_types_and_without_secrets(
        self, now, client_factory, respond
    ):
        for proposed_types, valid in (
            ("aws:ec2:instance,aws:lambda:function", True),
            ("aws:lambda:function", False),
        ):
            with self.subTest(proposed_types=proposed_types):
                self.client.execute_change_set.reset_mock()
                self.client.describe_change_set.return_value = {
                    "Status": "CREATE_IN_PROGRESS",
                    "ExecutionStatus": "UNAVAILABLE",
                }
                lambda_client = Mock()
                client_factory.side_effect = [self.client, lambda_client]
                context = Mock(invoked_function_arn="launcher-function")
                context.get_remaining_time_in_millis.return_value = 80_000
                request = event("Update")
                request["ResourceProperties"].update(
                    InstrumentationResourceTypes="aws:lambda:function",
                    APPKey="test-app-secret",
                )
                request["OldResourceProperties"] = {"APIKey": "old-api-secret"}
                launcher.wait_for_result(request, context, state())
                raw = lambda_client.invoke.call_args.kwargs["Payload"].decode()
                continuation = json.loads(raw)
                self.assertEqual(
                    continuation["ResourceProperties"],
                    {
                        "AccountId": ACCOUNT,
                        "InstrumentationResourceTypes": "aws:lambda:function",
                    },
                )
                self.assertNotIn("OldResourceProperties", continuation)
                for secret in (
                    "test-api-secret",
                    "test-app-secret",
                    "old-api-secret",
                    PROPS["ExternalId"],
                ):
                    self.assertNotIn(secret, raw)
                available = {
                    "Status": "CREATE_COMPLETE",
                    "ExecutionStatus": "AVAILABLE",
                    "ChangeSetId": CHANGE_SET,
                    "IncludeNestedStacks": True,
                    "Parameters": [
                        {
                            "ParameterKey": "InstrumentationResourceTypes",
                            "ParameterValue": proposed_types,
                        }
                    ],
                    "Changes": [],
                }
                self.client.describe_change_set.return_value = available
                self.pages[CHANGE_SET] = [available]
                if valid:
                    self.assertFalse(
                        launcher.poll_change_set(
                            self.client, continuation, continuation["LauncherState"]
                        )
                    )
                    self.client.execute_change_set.assert_called_once_with(
                        StackName=ROOT,
                        ChangeSetName=CHANGE_SET,
                        ClientRequestToken=launcher.request_name(request),
                        DisableRollback=False,
                    )
                    self.assertEqual(
                        continuation["LauncherState"]["Phase"], "deployment"
                    )
                else:
                    with self.assertRaisesRegex(
                        launcher.InstallationError, "resource types"
                    ):
                        launcher.poll_change_set(
                            self.client, continuation, continuation["LauncherState"]
                        )
                    self.client.execute_change_set.assert_not_called()
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "time", return_value=10)
    def test_delete_continuation_does_not_require_requested_types(
        self, now, client_factory, respond
    ):
        self.client.describe_stacks.return_value = {
            "Stacks": [stack(StackStatus="UPDATE_IN_PROGRESS")]
        }
        lambda_client = Mock()
        client_factory.side_effect = [self.client, lambda_client]
        context = Mock(invoked_function_arn="launcher-function")
        context.get_remaining_time_in_millis.return_value = 80_000
        request = event("Delete")
        request["ResourceProperties"] = {"AccountId": ACCOUNT}
        launcher.wait_for_result(request, context, state("delete"))
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["ResourceProperties"], {"AccountId": ACCOUNT})
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "time", return_value=10)
    def test_completed_request_reports_the_target_not_just_change_set_creation(
        self, now, client_factory, respond
    ):
        client_factory.return_value = self.client
        self.client.get_paginator.return_value.paginate.return_value = [
            {
                "StackEvents": [
                    {
                        "PhysicalResourceId": ROOT,
                        "ClientRequestToken": launcher.request_name(event()),
                        "ResourceStatus": "UPDATE_COMPLETE",
                    }
                ]
            }
        ]
        launcher.wait_for_result(event(), Mock(), state("deployment"))
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.SUCCESS)
        self.assertEqual(respond.call_args.args[4]["InstallationStackId"], ROOT)

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "sleep")
    @patch.object(launcher.time, "time", return_value=10)
    def test_delete_waits_for_active_target_before_releasing_helper_resources(
        self, now, sleep, client_factory, respond
    ):
        client_factory.return_value = self.client
        self.client.describe_stacks.side_effect = [
            {"Stacks": [stack(StackStatus="UPDATE_IN_PROGRESS")]},
            {"Stacks": [stack(StackStatus="UPDATE_COMPLETE")]},
        ]
        context = Mock()
        context.get_remaining_time_in_millis.return_value = 120_000
        launcher.wait_for_result(event("Delete"), context, state("delete"))
        sleep.assert_called_once_with(5)
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.SUCCESS)
        self.client.delete_stack.assert_not_called()
        self.client.execute_change_set.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher.boto3, "client")
    @patch.object(launcher.time, "time", return_value=1001)
    def test_timeout_is_not_reported_as_success(self, now, client_factory, respond):
        with self.assertRaisesRegex(launcher.InstallationError, "Timed out"):
            launcher.wait_for_result(event(), Mock(), state())
        respond.assert_not_called()


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        client_factory = patch.object(
            launcher.boto3, "client", return_value=self.client
        )
        self.client_factory = client_factory.start()
        self.addCleanup(client_factory.stop)
        self.history()

    def history(self, *statuses, stack_id=REQUEST):
        self.client.get_paginator.return_value.paginate.return_value = [
            {
                "StackEvents": [
                    {
                        "PhysicalResourceId": stack_id,
                        "ResourceType": "AWS::CloudFormation::Stack",
                        "ResourceStatus": status,
                    }
                    for status in statuses
                ]
            }
        ]

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_completed_launcher_deletes_without_discovering_another_active_update(
        self, factory, wait, respond
    ):
        factory.return_value.find_stack.return_value = stack(
            StackStatus="UPDATE_IN_PROGRESS"
        )
        for status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
            with self.subTest(status=status):
                self.history("DELETE_IN_PROGRESS", "DELETE_FAILED", status)
                launcher.handler(event("Delete"), Mock())
                self.assertEqual(
                    respond.call_args.args[3], launcher.cfnresponse.SUCCESS
                )
        factory.assert_not_called()
        wait.assert_not_called()
        self.client_factory.assert_called_with(
            "cloudformation", region_name="us-east-1", config=launcher.SDK_CONFIG
        )
        self.client.get_paginator.return_value.paginate.assert_called_with(
            StackName=REQUEST
        )
        self.client.delete_stack.assert_not_called()
        self.client.execute_change_set.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_old_launcher_success_does_not_clear_a_later_unfinished_or_failed_update(
        self, factory, wait, respond
    ):
        factory.return_value.find_stack.return_value = stack(
            StackStatus="UPDATE_IN_PROGRESS"
        )
        for status in (
            "CREATE_FAILED",
            "UPDATE_IN_PROGRESS",
            "UPDATE_ROLLBACK_COMPLETE",
        ):
            with self.subTest(status=status):
                wait.reset_mock()
                self.history("DELETE_IN_PROGRESS", status, "CREATE_COMPLETE")
                launcher.handler(event("Delete"), Mock())
                wait.assert_called_once()
                self.assertEqual(wait.call_args.args[2]["Phase"], "delete")
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_launcher_completion_checks_root_events_across_pages(
        self, factory, respond
    ):
        self.history("DELETE_IN_PROGRESS", "CREATE_COMPLETE")
        events = self.client.get_paginator.return_value.paginate.return_value[0][
            "StackEvents"
        ]
        self.client.get_paginator.return_value.paginate.return_value = [
            {
                "StackEvents": [
                    events[0],
                    {
                        **events[1],
                        "PhysicalResourceId": "nested",
                        "ResourceStatus": "CREATE_FAILED",
                    },
                    {
                        **events[1],
                        "ResourceType": "Custom::Agent",
                        "ResourceStatus": "CREATE_IN_PROGRESS",
                    },
                ]
            },
            {"StackEvents": [events[1]]},
        ]
        launcher.handler(event("Delete"), Mock())
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.SUCCESS)
        factory.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_denied_launcher_history_does_not_assume_completion(self, factory, respond):
        self.client.get_paginator.return_value.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "DescribeStackEvents",
        )
        launcher.handler(event("Delete"), Mock())
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)
        factory.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_deletion_waits_without_deleting_target(self, factory, wait, respond):
        factory.return_value.find_stack.return_value = stack(
            StackStatus="UPDATE_IN_PROGRESS"
        )
        request = event("Delete")
        launcher.handler(request, Mock())
        self.assertEqual(wait.call_args.args[2]["Phase"], "delete")
        factory.return_value.prepare.assert_not_called()
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_deletion_with_no_target_succeeds(self, factory, respond):
        factory.return_value.find_stack.return_value = None
        launcher.handler(event("Delete"), Mock())
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.SUCCESS)
        factory.return_value.prepare.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_preflight_recovery_link_is_visible_in_failure_reason(
        self, factory, respond
    ):
        factory.return_value.prepare.side_effect = launcher.InstallationError(
            "Pending first installation; inspect before deleting", ROOT
        )
        launcher.handler(event(), Mock())
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)
        self.assertIn(launcher.stack_url(ROOT), respond.call_args.kwargs["reason"])
        self.assertEqual(
            respond.call_args.args[4]["InstallationStackURL"], launcher.stack_url(ROOT)
        )

    @patch.object(launcher, "send_cfn_response")
    @patch.object(
        launcher,
        "wait_for_result",
        side_effect=launcher.InstallationError("Change set failed"),
    )
    @patch.object(launcher, "Launcher")
    def test_post_creation_failure_links_target_without_relying_on_outputs(
        self, factory, wait, respond
    ):
        factory.return_value.prepare.return_value = state()
        launcher.handler(event(), Mock())
        self.assertIn(launcher.stack_url(ROOT), respond.call_args.kwargs["reason"])
        self.assertNotIn("test-api-secret", respond.call_args.kwargs["reason"])

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher.boto3, "Session")
    @patch.object(launcher, "Launcher")
    def test_noncommercial_operations_fail_before_discovery_or_continuation(
        self, factory, session, wait, respond
    ):
        factory.return_value.prepare.return_value = state()
        for partition, kind, phase in product(
            ("aws-us-gov", "aws-cn"),
            ("Create", "Update"),
            (None, "change_set", "deployment"),
        ):
            with self.subTest(partition=partition, kind=kind, phase=phase):
                region = launcher.HOME_REGIONS[partition]
                for mock in (factory, session, wait, respond, self.client_factory):
                    mock.reset_mock()
                request = event(kind)
                request["StackId"] = REQUEST.replace(
                    "arn:aws:", f"arn:{partition}:"
                ).replace("us-east-1", region)
                request["ResourceProperties"]["InstrumentationResourceTypes"] = (
                    "aws:ecs:cluster"
                )
                if phase:
                    request["ResourceProperties"] = {"AccountId": ACCOUNT}
                    request["LauncherState"] = {
                        **state(phase=phase),
                        "Region": region,
                        "StackId": ROOT.replace(
                            "arn:aws:", f"arn:{partition}:"
                        ).replace("us-east-1", region),
                    }
                launcher.handler(request, Mock())
                factory.assert_not_called()
                session.assert_not_called()
                self.client_factory.assert_not_called()
                wait.assert_not_called()
                respond.assert_called_once()
                self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)
                self.assertIn(
                    "supports only commercial AWS accounts",
                    respond.call_args.kwargs["reason"],
                )

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_commercial_create_and_update_still_prepare_installation(
        self, factory, wait, respond
    ):
        for kind in ("Create", "Update"):
            with self.subTest(kind=kind):
                factory.reset_mock()
                wait.reset_mock()
                factory.return_value.prepare.return_value = state()
                request = event(kind)
                launcher.handler(request, Mock())
                self.assertEqual(factory.call_args.args[1], "aws")
                factory.return_value.prepare.assert_called_once_with(request)
                wait.assert_called_once()
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_noncommercial_deletion_preserves_existing_installations(
        self, factory, wait, respond
    ):
        for partition in ("aws-us-gov", "aws-cn"):
            region = launcher.HOME_REGIONS[partition]
            for has_target in (False, True):
                with self.subTest(partition=partition, has_target=has_target):
                    for mock in (factory, wait, respond, self.client_factory):
                        mock.reset_mock()
                    request = event("Delete")
                    request["StackId"] = REQUEST.replace(
                        "arn:aws:", f"arn:{partition}:"
                    ).replace("us-east-1", region)
                    target_id = ROOT.replace("arn:aws:", f"arn:{partition}:").replace(
                        "us-east-1", region
                    )
                    factory.return_value.find_stack.return_value = (
                        stack(StackId=target_id, StackStatus="UPDATE_IN_PROGRESS")
                        if has_target
                        else None
                    )
                    launcher.handler(request, Mock())
                    self.assertEqual(factory.call_args.args[1], partition)
                    factory.return_value.prepare.assert_not_called()
                    self.client_factory.assert_called_once_with(
                        "cloudformation", region_name=region, config=launcher.SDK_CONFIG
                    )
                    if has_target:
                        wait.assert_called_once()
                        self.assertEqual(wait.call_args.args[2]["Phase"], "delete")
                        self.assertEqual(wait.call_args.args[2]["StackId"], target_id)
                        respond.assert_not_called()
                    else:
                        wait.assert_not_called()
                        self.assertEqual(
                            respond.call_args.args[3], launcher.cfnresponse.SUCCESS
                        )
        self.client.delete_stack.assert_not_called()
        self.client.execute_change_set.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_completed_noncommercial_launcher_can_be_deleted(
        self, factory, wait, respond
    ):
        for partition in ("aws-us-gov", "aws-cn"):
            with self.subTest(partition=partition):
                respond.reset_mock()
                region = launcher.HOME_REGIONS[partition]
                request = event("Delete")
                request["StackId"] = REQUEST.replace(
                    "arn:aws:", f"arn:{partition}:"
                ).replace("us-east-1", region)
                self.history(
                    "DELETE_IN_PROGRESS", "CREATE_COMPLETE", stack_id=request["StackId"]
                )
                launcher.handler(request, Mock())
                self.assertEqual(
                    respond.call_args.args[3], launcher.cfnresponse.SUCCESS
                )
        factory.assert_not_called()
        wait.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "wait_for_result")
    @patch.object(launcher, "Launcher")
    def test_noncommercial_delete_continuations_remain_allowed(
        self, factory, wait, respond
    ):
        for partition in ("aws-us-gov", "aws-cn"):
            with self.subTest(partition=partition):
                wait.reset_mock()
                region = launcher.HOME_REGIONS[partition]
                request = event("Delete")
                request["StackId"] = REQUEST.replace(
                    "arn:aws:", f"arn:{partition}:"
                ).replace("us-east-1", region)
                request["ResourceProperties"] = {"AccountId": ACCOUNT}
                request["LauncherState"] = {
                    **state(phase="delete"),
                    "Region": region,
                    "StackId": ROOT.replace("arn:aws:", f"arn:{partition}:").replace(
                        "us-east-1", region
                    ),
                }
                context = Mock()
                launcher.handler(request, context)
                wait.assert_called_once_with(request, context, request["LauncherState"])
        factory.assert_not_called()
        self.client_factory.assert_not_called()
        respond.assert_not_called()

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_wrong_account_never_gets_aws_clients(self, factory, respond):
        request = event()
        request["ResourceProperties"]["AccountId"] = "999999999999"
        launcher.handler(request, Mock())
        factory.assert_not_called()
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_expired_request_cannot_start_an_operation(self, factory, respond):
        request = event()
        request["ResponseURL"] = (
            "https://example.invalid/?X-Amz-Date=20200101T000000Z&X-Amz-Expires=3600"
        )
        launcher.handler(request, Mock())
        factory.assert_not_called()
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)
        self.assertIn("expired", respond.call_args.kwargs["reason"])

    @patch.object(launcher, "send_cfn_response")
    @patch.object(launcher, "Launcher")
    def test_failure_response_does_not_leak_aws_errors_or_credentials(
        self, factory, respond
    ):
        factory.side_effect = RuntimeError("test-api-secret signed-response")
        launcher.handler(event(), Mock())
        data = json.dumps(respond.call_args.args[4])
        self.assertNotIn("test-api-secret", data)
        self.assertNotIn("signed-response", data)
        self.assertEqual(respond.call_args.args[3], launcher.cfnresponse.FAILED)


class TemplateTest(unittest.TestCase):
    def load(self, filename):
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_multi_constructor(
            "!",
            lambda loader, tag, node: (
                loader.construct_mapping(node)
                if isinstance(node, yaml.MappingNode)
                else loader.construct_sequence(node)
                if isinstance(node, yaml.SequenceNode)
                else loader.construct_scalar(node)
            ),
        )
        return yaml.load(Path(__file__).with_name(filename).read_text(), Loader=Loader)

    def test_launcher_logging_cannot_recreate_its_log_group(self):
        template = self.load("main_agent_installation_launcher.yaml")
        resources = template["Resources"]
        role = resources["LauncherRole"]["Properties"]
        self.assertNotIn("ManagedPolicyArns", role)
        statements = role["Policies"][0]["PolicyDocument"]["Statement"]
        logging = next(s for s in statements if s["Sid"] == "LauncherLogging")
        self.assertEqual(
            logging,
            {
                "Sid": "LauncherLogging",
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": "LauncherLogGroup.Arn",
            },
        )
        for statement in statements:
            if statement is logging:
                continue
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            self.assertFalse(
                any(action.startswith("logs:") or action == "*" for action in actions)
            )
        self.assertEqual(resources["LauncherFunction"]["DependsOn"], "LauncherLogGroup")
        self.assertEqual(
            resources["LauncherLogGroup"]["Properties"]["RetentionInDays"], 14
        )
        self.assertNotIn("DeletionPolicy", resources["LauncherLogGroup"])

    def test_patterns_match_forwarding_template(self):
        resources = self.load("datadog_agent_resource_update_forwarding.yaml")[
            "Resources"
        ]
        for resource_type in launcher.FORWARDED_TYPES:
            service = resource_type.split(":")[1].upper()
            self.assertEqual(
                launcher.expected_pattern(resource_type),
                resources["DDEventForwardingRule" + service]["Properties"][
                    "EventPattern"
                ],
            )

    def test_supplementary_template_only_owns_rules_and_their_invocation_role(self):
        template = self.load("datadog_agent_supplementary_forwarding.yaml")
        self.assertEqual(
            set(template["Parameters"]),
            {"ExistingApiDestinationArn", "InstrumentationResourceTypes"},
        )
        self.assertEqual(
            template["Conditions"],
            self.load("datadog_agent_resource_update_forwarding.yaml")["Conditions"],
        )
        resources = template["Resources"]
        self.assertEqual(len(resources), 4)
        role = resources["DDEventBridgeInvocationRole"]["Properties"]
        self.assertNotIn("RoleName", role)
        statement = role["Policies"][0]["PolicyDocument"]["Statement"][0]
        self.assertEqual(
            statement,
            {
                "Effect": "Allow",
                "Action": "events:InvokeApiDestination",
                "Resource": "ExistingApiDestinationArn",
            },
        )
        trust = role["AssumeRolePolicyDocument"]["Statement"][0]["Condition"]
        self.assertEqual(trust["StringEquals"], {"aws:SourceAccount": "AWS::AccountId"})
        self.assertIn(
            "datadog-agent-supp-${Id}-*", trust["ArnLike"]["aws:SourceArn"][0]
        )
        for rt in launcher.FORWARDED_TYPES:
            service = rt.split(":")[1]
            rule = resources["DDEventForwardingRule" + service.upper()]
            self.assertEqual(rule["Type"], "AWS::Events::Rule")
            self.assertEqual(rule["Condition"], "Include" + service.upper())
            props = rule["Properties"]
            self.assertEqual(props["EventPattern"], launcher.expected_pattern(rt))
            self.assertEqual(props["State"], "ENABLED")
            self.assertEqual(
                props["Targets"],
                [
                    {
                        "Id": "datadog-intake",
                        "Arn": "ExistingApiDestinationArn",
                        "RoleArn": "DDEventBridgeInvocationRole.Arn",
                    }
                ],
            )
            self.assertIn(
                {"Key": launcher.SUPPLEMENTARY_OWNER_TAG, "Value": "AWS::StackId"},
                props["Tags"],
            )
            pattern, variables = props["Name"]
            self.assertEqual(variables["Id"], [2, ["/", "AWS::StackId"]])
            name = pattern.replace("${Id}", SUPPLEMENTARY.rsplit("/", 1)[1])
            self.assertEqual(name, launcher.supplementary_rule_name(SUPPLEMENTARY, rt))
            self.assertLessEqual(len(name), 64)
            self.assertFalse(name.startswith(launcher.RULE_PREFIX))
        for resource in resources.values():
            self.assertNotIn("DeletionPolicy", resource)
            self.assertNotIn("UpdateReplacePolicy", resource)

    def test_supplementary_lifecycle_is_nested_and_does_not_receive_credentials(self):
        template = self.load("main_agent_installation.yaml")
        child = template["Resources"][launcher.SUPPLEMENTARY_RESOURCE]
        self.assertEqual(child["Condition"], "HasSupplementaryForwarding")
        self.assertEqual(
            child["Properties"]["Parameters"],
            {
                "ExistingApiDestinationArn": "ExistingApiDestinationArn",
                "InstrumentationResourceTypes": "SupplementaryResourceTypes",
            },
        )
        self.assertTrue(
            child["Properties"]["TemplateURL"].endswith(
                "/datadog_agent_supplementary_forwarding.yaml"
            )
        )
        self.assertNotIn("DeletionPolicy", child)
        self.assertNotIn("UpdateReplacePolicy", child)
        for key in ("SupplementaryResourceTypes", "ExistingApiDestinationArn"):
            self.assertEqual(template["Parameters"][key]["Default"], "")
        self.assertIn("ValidateSupplementaryForwarding", template["Rules"])

    def test_supplementary_rule_writes_require_cloudformation_and_own_namespace(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        lifecycle = next(
            s for s in statements if s["Sid"] == "SupplementaryRuleLifecycle"
        )
        self.assertTrue(lifecycle["Resource"].endswith(":rule/datadog-agent-supp-*"))
        self.assertIn(
            "cloudformation.amazonaws.com",
            lifecycle["Condition"]["ForAnyValue:StringEquals"]["aws:CalledVia"],
        )
        self.assertNotIn("events:UpdateApiDestination", lifecycle["Action"])
        self.assertNotIn("events:UpdateConnection", lifecycle["Action"])
        discovery = next(
            s for s in statements if s["Sid"] == "SupplementaryRuleDiscovery"
        )
        self.assertEqual(
            set(discovery["Action"]),
            {
                "events:DescribeRule",
                "events:ListTargetsByRule",
                "events:ListTagsForResource",
            },
        )
        templates = next(s for s in statements if s["Sid"] == "VersionedTemplates")
        self.assertTrue(
            any(
                url.endswith("/datadog_agent_supplementary_forwarding.yaml")
                for url in templates["Resource"]
            )
        )

    def test_forwarding_discovery_uses_name_only_arns_without_lifecycle_writes(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        discovery = next(s for s in statements if s["Sid"] == "ForwardingDiscovery")
        names = [
            (
                "arn:${AWS::Partition}:events:*:${AWS::AccountId}:"
                "connection/datadog-agent-resource-update-intake-connection"
            ),
            (
                "arn:${AWS::Partition}:events:*:${AWS::AccountId}:"
                "api-destination/datadog-agent-resource-update-intake-destination"
            ),
        ]
        self.assertEqual(
            discovery,
            {
                "Sid": "ForwardingDiscovery",
                "Effect": "Allow",
                "Action": [
                    "events:DescribeConnection",
                    "events:DescribeApiDestination",
                ],
                "Resource": names,
            },
        )
        lifecycle = next(s for s in statements if s["Sid"] == "ForwardingLifecycle")
        self.assertEqual(
            lifecycle["Resource"],
            [
                *(name + "/*" for name in names),
                (
                    "arn:${AWS::Partition}:events:*:${AWS::AccountId}:"
                    "rule/datadog-agent-resource-update-rule-*"
                ),
            ],
        )

    def test_forwarding_pipeline_writes_use_exact_names_via_cloudformation(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        lifecycle = next(
            s for s in statements if s["Sid"] == "ForwardingPipelineLifecycle"
        )
        self.assertEqual(
            lifecycle["Action"],
            [
                "events:CreateConnection",
                "events:UpdateConnection",
                "events:DeleteConnection",
                "events:CreateApiDestination",
                "events:UpdateApiDestination",
                "events:DeleteApiDestination",
            ],
        )
        discovery = next(s for s in statements if s["Sid"] == "ForwardingDiscovery")
        self.assertEqual(lifecycle["Resource"], discovery["Resource"])
        self.assertEqual(
            lifecycle["Condition"],
            {
                "ForAnyValue:StringEquals": {
                    "aws:CalledVia": ["cloudformation.amazonaws.com"]
                }
            },
        )

    def test_connection_secret_access_requires_cloudformation_then_eventbridge(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        secrets = next(
            s for s in statements if s["Sid"] == "ForwardingConnectionSecret"
        )
        self.assertEqual(
            secrets,
            {
                "Sid": "ForwardingConnectionSecret",
                "Effect": "Allow",
                "Action": [
                    "secretsmanager:CreateSecret",
                    "secretsmanager:UpdateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:PutSecretValue",
                ],
                "Resource": "arn:${AWS::Partition}:secretsmanager:*:${AWS::AccountId}:"
                "secret:events!connection/datadog-agent-resource-update-intake-connection/*",
                "Condition": {
                    "StringEquals": {
                        "aws:CalledViaFirst": "cloudformation.amazonaws.com",
                        "aws:CalledViaLast": "events.amazonaws.com",
                    },
                },
            },
        )
        for statement in statements:
            if statement is secrets:
                continue
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            self.assertFalse(
                any(action.startswith("secretsmanager:") for action in actions)
            )

    def test_physical_role_owner_lookup_has_only_unscoped_resource_read(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        discovery = next(
            s for s in statements if s["Sid"] == "PhysicalRoleOwnerDiscovery"
        )
        self.assertEqual(
            discovery,
            {
                "Sid": "PhysicalRoleOwnerDiscovery",
                "Effect": "Allow",
                "Action": "cloudformation:DescribeStackResources",
                "Resource": "*",
            },
        )
        unscoped_cloudformation_actions = set()
        for statement in statements:
            if statement["Resource"] != "*":
                continue
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            unscoped_cloudformation_actions.update(
                action for action in actions if action.startswith("cloudformation:")
            )
        self.assertEqual(
            unscoped_cloudformation_actions, {"cloudformation:DescribeStackResources"}
        )
        owner_stacks = next(
            s for s in statements if s["Sid"] == "ForwardingOwnerDiscovery"
        )
        self.assertEqual(
            owner_stacks["Resource"],
            "arn:${AWS::Partition}:cloudformation:*:${AWS::AccountId}:stack/*/*",
        )

    def test_launcher_deployment_history_is_scoped_to_its_own_stack(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        history = next(s for s in statements if s["Sid"] == "LauncherDeploymentHistory")
        self.assertEqual(history["Action"], "cloudformation:DescribeStackEvents")
        self.assertEqual(history["Resource"], "AWS::StackId")

    def test_refresh_token_reaches_the_permission_custom_resource(self):
        installation = self.load("main_agent_installation.yaml")
        nested = installation["Resources"][launcher.OWNED_PERMISSIONS_RESOURCE][
            "Properties"
        ]["Parameters"]
        self.assertEqual(
            nested["InstrumentationRefreshToken"], "InstrumentationRefreshToken"
        )
        permissions_template = self.load("datadog_integration_permissions.yaml")
        trigger = permissions_template["Resources"][
            "DatadogAttachIntegrationPermissionsFunctionTrigger"
        ]["Properties"]
        self.assertEqual(
            trigger["InstrumentationRefreshToken"],
            [
                "HasInstrumentationRefreshToken",
                "InstrumentationRefreshToken",
                "AWS::NoValue",
            ],
        )

    def test_addon_uses_isolated_policies_without_changing_quickstart_default(self):
        main = self.load("main_agent_installation.yaml")
        child = self.load("datadog_integration_permissions.yaml")
        self.assertEqual(
            main["Resources"][launcher.PERMISSIONS_RESOURCE]["Properties"][
                "Parameters"
            ]["InstrumentationPolicyScope"],
            ["HasLegacyPermissions", "AWS::NoValue", "stack"],
        )
        self.assertEqual(
            child["Parameters"]["InstrumentationPolicyScope"]["Default"], "role"
        )
        self.assertEqual(
            child["Resources"]["DatadogAttachIntegrationPermissionsFunctionTrigger"][
                "Properties"
            ]["InstrumentationPolicyScope"],
            ["HasStackScopedInstrumentation", "stack", "AWS::NoValue"],
        )
        self.assertIn("ExternalId", main["Parameters"])
        entry = self.load("main_agent_installation_launcher.yaml")
        self.assertEqual(
            entry["Resources"]["LaunchInstallation"]["Properties"]["ExternalId"],
            "ExternalId",
        )

    def test_installation_manifest_matches_the_approved_root_resources(self):
        resources = self.load("main_agent_installation.yaml")["Resources"]
        self.assertEqual(set(resources), launcher.INSTALLATION_RESOURCES)
        self.assertTrue(
            all(
                resource["Type"] == "AWS::CloudFormation::Stack"
                for resource in resources.values()
            )
        )

    def test_target_enforces_launcher_parameter_constraints(self):
        entry = self.load("main_agent_installation_launcher.yaml")["Parameters"]
        target = self.load("main_agent_installation.yaml")["Parameters"]
        for name in (
            "DatadogSite",
            "IAMRoleName",
            "AccountId",
            "InstrumentationResourceTypes",
        ):
            for key in ("AllowedValues", "AllowedPattern", "MaxLength"):
                if key in entry[name]:
                    with self.subTest(parameter=name, constraint=key):
                        self.assertEqual(target[name][key], entry[name][key])

    def test_cloudformation_writes_require_approved_templates(self):
        template = self.load("main_agent_installation_launcher.yaml")
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        template_url = template["Resources"]["LauncherFunction"]["Properties"][
            "Environment"
        ]["Variables"]["INSTALLATION_TEMPLATE_URL"]
        writers = []
        for statement in statements:
            actions = statement["Action"]
            if isinstance(actions, str):
                actions = [actions]
            if set(actions) & {
                "cloudformation:CreateChangeSet",
                "cloudformation:CreateStack",
                "cloudformation:UpdateStack",
            }:
                conditions = statement["Condition"]
                patterns = conditions.get(
                    "StringEquals", conditions.get("StringLike", {})
                )["cloudformation:TemplateUrl"]
                writers.append(
                    (
                        actions,
                        conditions,
                        patterns if isinstance(patterns, list) else [patterns],
                    )
                )
        self.assertEqual(len(writers), 2)

        def allowed(action, url, via=()):
            if url is None:
                return False
            return any(
                action in actions
                and any(fnmatchcase(url, pattern) for pattern in patterns)
                and (
                    "ForAnyValue:StringEquals" not in conditions
                    or set(via)
                    & set(conditions["ForAnyValue:StringEquals"]["aws:CalledVia"])
                )
                for actions, conditions, patterns in writers
            )

        self.assertTrue(allowed("cloudformation:CreateChangeSet", template_url))
        for action in (
            "cloudformation:CreateChangeSet",
            "cloudformation:CreateStack",
            "cloudformation:UpdateStack",
        ):
            for url in (
                None,
                "https://attacker.invalid/template.yaml",
                template_url.replace("main_agent_installation.yaml", "main.yaml"),
            ):
                with self.subTest(action=action, url=url):
                    self.assertFalse(
                        allowed(action, url, ["cloudformation.amazonaws.com"])
                    )
        self.assertFalse(
            allowed(
                "cloudformation:CreateChangeSet",
                template_url.replace("<VERSION_PLACEHOLDER>", "v4.21.1"),
            )
        )
        for filename in (
            "datadog_integration_permissions.yaml",
            "datadog_agent_resource_update_forwarding.yaml",
            "datadog_agent_supplementary_forwarding.yaml",
        ):
            for version in ("v4.23.0", "v4.22.0", "v4.21.1"):
                url = template_url.replace(
                    "main_agent_installation.yaml", filename
                ).replace("<VERSION_PLACEHOLDER>", version)
                for action in (
                    "cloudformation:CreateChangeSet",
                    "cloudformation:CreateStack",
                    "cloudformation:UpdateStack",
                ):
                    with self.subTest(
                        action=action, filename=filename, version=version
                    ):
                        self.assertFalse(allowed(action, url))
                        self.assertTrue(
                            allowed(action, url, ["cloudformation.amazonaws.com"])
                        )
        nested = next(
            statement
            for statement in statements
            if statement["Sid"] == "NestedStackLifecycle"
        )
        self.assertIn("aws:CalledVia", nested["Condition"]["ForAnyValue:StringEquals"])

    def test_launcher_parameters_match_api_key_only_installation_template(self):
        target = self.load("main_agent_installation.yaml")
        for previous in (
            None,
            {"APIKey": "previous-api-key", "APPKey": "obsolete-app-key"},
        ):
            with self.subTest(updating=previous is not None):
                result = launcher.change_set_parameters(
                    PROPS,
                    {"aws:ec2:instance"},
                    launcher.ForwardingPlan(),
                    "request-token",
                    previous,
                    {},
                )
                values = {item["ParameterKey"]: item for item in result}
                self.assertLessEqual(set(values), set(target["Parameters"]))
                self.assertNotIn("APPKey", values)
                self.assertEqual(
                    values["APIKey"],
                    {"ParameterKey": "APIKey", "ParameterValue": PROPS["APIKey"]}
                    if previous is None
                    else {"ParameterKey": "APIKey", "UsePreviousValue": True},
                )

    def test_no_retained_or_coupled_installation_lifecycle(self):
        template = self.load("main_agent_installation_launcher.yaml")
        self.assertNotIn("DatadogAgentInstallation", template["Resources"])
        source = Path(__file__).with_name("launch_agent_installation.py").read_text()
        self.assertNotIn("delete_stack(", source)
        self.assertNotIn("RoleARN=", source)
        self.assertTrue(template["Parameters"]["APIKey"]["NoEcho"])
        self.assertNotIn("APPKey", template["Parameters"])
        self.assertNotIn(
            "APPKey", template["Resources"]["LaunchInstallation"]["Properties"]
        )
        self.assertEqual(
            template["Resources"]["LaunchInstallation"]["Properties"]["ServiceTimeout"],
            3600,
        )
        role = template["Resources"]["LauncherRole"]["Properties"]
        statements = role["Policies"][0]["PolicyDocument"]["Statement"]
        delete = next(
            item for item in statements if item["Sid"] == "NestedStackLifecycle"
        )
        self.assertIn(
            "stack/DatadogAgentInstallation-*/*", json.dumps(delete["Resource"])
        )
        self.assertNotIn("AdministratorAccess", json.dumps(role))
        for sid in (
            "InstallationRoles",
            "InstallationRolePassing",
            "InstallationFunctions",
        ):
            statement = next(item for item in statements if item["Sid"] == sid)
            self.assertIn(
                "cloudformation.amazonaws.com",
                statement["Condition"]["ForAnyValue:StringEquals"]["aws:CalledVia"],
            )
        direct_reads = next(
            item for item in statements if item["Sid"] == "ForwardingRoleDiscovery"
        )
        self.assertEqual(direct_reads["Action"], "iam:GetRole")


if __name__ == "__main__":
    unittest.main()
