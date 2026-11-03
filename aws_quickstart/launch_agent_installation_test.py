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
QUICKSTART_ROOT = ROOT.replace("DatadogAgentInstallation/", "DatadogIntegration/")
QUICKSTART_FORWARDING = FORWARDING.replace(
    "DatadogAgentInstallation-", "DatadogIntegration-"
)
TEMPLATE_URL = (
    "https://test-bucket.s3.amazonaws.com/aws/v4.22.0/main_agent_installation.yaml"
)
PROPS = {
    "AccountId": ACCOUNT,
    "IAMRoleName": "DatadogIntegrationRole",
    "DatadogSite": "datadoghq.com",
    "ExternalId": "current-org-external-id",
    "InstrumentationResourceTypes": "aws:ec2:instance",
    "APIKey": "test-api-secret",
    "APPKey": "test-app-secret",
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
            {"INSTALLATION_TEMPLATE_URL": TEMPLATE_URL, "TEMPLATE_VERSION": "v4.22.0"},
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
        self.permissions_scope = "stack"
        self.owner_identity = dict(PROPS)
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

    def describe_stack_resources(self, *, StackName):
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
        if owner:
            tags.append({"Key": "aws:cloudformation:stack-id", "Value": owner})
        self.iam.get_role.side_effect = None
        self.iam.get_role.return_value = {"Role": {"Tags": tags}}
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
            "Targets": [{"Arn": DESTINATION_ARN, "RoleArn": "invocation-role"}]
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
                    else "invocation-role",
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
        current["Parameters"].append(
            {"ParameterKey": "FutureSetting", "ParameterValue": "keep"}
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
        for key in ("APIKey", "APPKey", "FutureSetting"):
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
        self.forwarding("different-stack")
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
            "Stacks": [{"StackId": owner, "RootId": "renamed-installation-root"}]
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
            "Stacks": [{"StackId": owner, "RootId": "renamed-root"}]
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
            "Stacks": [{"StackId": owner, "RootId": "quickstart-root"}]
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
        self.cf.describe_stack_resources.assert_called_once_with(
            StackName="quickstart-root"
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
                    "Value": "v4.23.0",
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

    def test_untagged_external_forwarding_is_not_assumed_compatible(self):
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
        self.assertEqual(payload["ResourceProperties"], {"AccountId": ACCOUNT})
        self.assertNotIn("test-api-secret", args["Payload"].decode())
        self.assertNotIn("test-app-secret", args["Payload"].decode())
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
            for version in ("v4.22.0", "v4.21.1"):
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

    def test_no_retained_or_coupled_installation_lifecycle(self):
        template = self.load("main_agent_installation_launcher.yaml")
        self.assertNotIn("DatadogAgentInstallation", template["Resources"])
        source = Path(__file__).with_name("launch_agent_installation.py").read_text()
        self.assertNotIn("delete_stack(", source)
        self.assertNotIn("RoleARN=", source)
        self.assertTrue(template["Parameters"]["APIKey"]["NoEcho"])
        self.assertTrue(template["Parameters"]["APPKey"]["NoEcho"])
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
