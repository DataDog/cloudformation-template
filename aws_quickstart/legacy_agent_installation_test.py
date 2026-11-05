import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from botocore.exceptions import ClientError

import launch_agent_installation_test as existing_tests

launcher = existing_tests.launcher
ROOT = existing_tests.ROOT
PROPS = existing_tests.PROPS
ROLE_STACK = existing_tests.QUICKSTART_ROOT.replace("/target", "-role/nested")


class TemplateLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    value = getattr(loader, f"construct_{node.id}")(node)
    return {tag if tag in {"Ref", "Condition"} else f"Fn::{tag}": value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def legacy_template(prefix="https://test-bucket.s3.amazonaws.com", version="v4.21.1"):
    source = (
        Path(__file__).parent / "fixtures/main_agent_installation_v4_21_1.yaml"
    ).read_text()
    return source.replace(
        "https://<BUCKET_PLACEHOLDER>.s3.amazonaws.com", prefix
    ).replace("<VERSION_PLACEHOLDER>", version)


def stack_parameters(values):
    return [
        {"ParameterKey": key, "ParameterValue": value} for key, value in values.items()
    ]


class LegacyAdoptionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = existing_tests.LauncherTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.existing(
            Parameters=stack_parameters(
                {
                    **{
                        key: value
                        for key, value in PROPS.items()
                        if key != "ExternalId"
                    },
                    "APPKey": "legacy-app-secret",
                }
            )
        )
        self.legacy_parameters = {
            "IAMRoleName": PROPS["IAMRoleName"],
            "DatadogSite": PROPS["DatadogSite"],
            "InstrumentationResourceTypes": PROPS["InstrumentationResourceTypes"],
            "ResourceCollectionPermissions": "false",
            "ManageBasePermissions": "false",
            "FailOnInstrumentationError": "true",
            "AgentlessScanning": "false",
        }
        self.role_parameters = {
            key: PROPS[key] for key in ("IAMRoleName", "DatadogSite", "ExternalId")
        }
        self.role_parameters["DdAWSAccountId"] = "464622532012"
        self.role_parent = existing_tests.QUICKSTART_ROOT
        self.role_name = PROPS["IAMRoleName"]
        self.owned_scope = "stack"
        f.role_owners[PROPS["IAMRoleName"]] = ROLE_STACK
        self.role = {
            "RoleName": PROPS["IAMRoleName"],
            "CreateDate": existing_tests.ROLE_CREATED,
            "Tags": [],
            "AssumeRolePolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["sts:AssumeRole"],
                        "Principal": {"AWS": "arn:aws:iam::464622532012:root"},
                        "Condition": {
                            "StringEquals": {"sts:ExternalId": PROPS["ExternalId"]}
                        },
                    }
                ],
            },
        }
        self.legacy_parent = ROOT
        self.pipeline = False
        f.cf.get_template.return_value = {"TemplateBody": legacy_template()}
        f.cf.describe_stacks.side_effect = self.describe_stacks
        f.cf.describe_stack_resources.side_effect = self.describe_resources
        f.iam.get_role.side_effect = self.get_role

    def describe_stacks(self, *, StackName):
        if StackName == "permissions":
            return {
                "Stacks": [
                    {
                        "StackId": "permissions",
                        "ParentId": self.legacy_parent,
                        "Parameters": stack_parameters(self.legacy_parameters),
                    }
                ]
            }
        if StackName == ROLE_STACK:
            return {
                "Stacks": [
                    {
                        "StackId": ROLE_STACK,
                        "StackStatus": "CREATE_COMPLETE",
                        "RootId": existing_tests.QUICKSTART_ROOT,
                        "ParentId": self.role_parent,
                        "Parameters": stack_parameters(self.role_parameters),
                    }
                ]
            }
        if StackName == "owned-permissions":
            return {
                "Stacks": [
                    {
                        "StackId": "owned-permissions",
                        "Parameters": stack_parameters(
                            {"InstrumentationPolicyScope": self.owned_scope}
                        ),
                    }
                ]
            }
        return self.fixture.describe_stacks(StackName=StackName)

    def describe_resources(
        self, *, StackName=None, PhysicalResourceId=None, LogicalResourceId=None
    ):
        if PhysicalResourceId is not None:
            return self.fixture.describe_stack_resources(
                PhysicalResourceId=PhysicalResourceId,
                LogicalResourceId=LogicalResourceId,
            )
        if StackName == existing_tests.QUICKSTART_ROOT:
            return {
                "StackResources": [
                    {
                        "LogicalResourceId": "DatadogIntegrationRoleStack",
                        "PhysicalResourceId": ROLE_STACK,
                        "ResourceType": "AWS::CloudFormation::Stack",
                    }
                ]
            }
        if StackName == ROLE_STACK:
            return {
                "StackResources": [
                    {
                        "LogicalResourceId": "DatadogIntegrationRole",
                        "ResourceType": "AWS::IAM::Role",
                        "PhysicalResourceId": self.role_name,
                    }
                ]
            }
        return self.fixture.describe_stack_resources(StackName=StackName)

    def get_role(self, *, RoleName):
        if RoleName == PROPS["IAMRoleName"]:
            return {"Role": self.role}
        if self.pipeline:
            return self.fixture.iam.get_role.return_value
        raise existing_tests.MissingResource()

    def forwarding(self, owner=existing_tests.QUICKSTART_FORWARDING):
        self.fixture.forwarding(owner)
        self.pipeline = True
        self.fixture.iam.get_role.side_effect = self.get_role
        self.fixture.coverage("aws:ec2:instance")

    def adopted(self):
        previous = launcher.parameters(self.fixture.current_stack)
        previous.pop("APPKey", None)
        previous.update(
            ExternalId=PROPS["ExternalId"],
            LegacyPermissionsTemplateVersion="v4.21.1",
            LegacyInstrumentationResourceTypes="aws:ec2:instance",
            InstrumentationResourceTypes="aws:ec2:instance,aws:eks:cluster",
        )
        self.fixture.current_stack["Parameters"] = stack_parameters(previous)
        self.fixture.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.OWNED_PERMISSIONS_RESOURCE,
                "PhysicalResourceId": "owned-permissions",
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )

    def assert_rejected(self):
        with self.assertRaises(launcher.InstallationError):
            self.fixture.subject.prepare(existing_tests.event())
        self.fixture.cf.create_change_set.assert_not_called()

    def test_adopts_the_same_root_and_freezes_legacy_inputs(self):
        request = existing_tests.event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:eks:cluster"
        )
        result = self.fixture.subject.prepare(request)
        args = self.fixture.cf.create_change_set.call_args.kwargs
        self.assertEqual(args["StackName"], ROOT)
        self.assertEqual(args["ChangeSetType"], "UPDATE")
        self.assertTrue(result["PreserveLegacyPermissions"])
        self.assertTrue(json.loads(args["Description"])["PreserveLegacyPermissions"])
        values = self.fixture.values()
        self.assertEqual(
            values["LegacyPermissionsTemplateVersion"]["ParameterValue"], "v4.21.1"
        )
        self.assertEqual(
            values["LegacyInstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance",
        )
        self.assertEqual(
            values["InstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance,aws:eks:cluster",
        )
        self.assertEqual(values["ExternalId"]["ParameterValue"], PROPS["ExternalId"])
        self.assertEqual(
            values["APIKey"], {"ParameterKey": "APIKey", "UsePreviousValue": True}
        )
        self.assertNotIn("APPKey", values)
        self.assertEqual(self.role["Tags"], [])
        self.fixture.cf.describe_stack_resources.assert_any_call(
            PhysicalResourceId=PROPS["IAMRoleName"],
            LogicalResourceId="DatadogIntegrationRole",
        )
        self.fixture.iam.attach_role_policy.assert_not_called()
        self.fixture.iam.detach_role_policy.assert_not_called()
        self.fixture.iam.tag_role.assert_not_called()
        self.fixture.cf.delete_stack.assert_not_called()

    def test_reuses_legacy_installation_owned_forwarding(self):
        self.forwarding(existing_tests.FORWARDING)
        self.fixture.cf.describe_stack_resources.return_value["StackResources"].append(
            {
                "LogicalResourceId": launcher.FORWARDING_RESOURCE,
                "PhysicalResourceId": existing_tests.FORWARDING,
                "ResourceType": "AWS::CloudFormation::Stack",
            }
        )
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["CreateResourceUpdateForwarding"]["ParameterValue"],
            "true",
        )

    def test_manual_quickstart_identity_is_resolved_from_its_role_child(self):
        self.forwarding()
        del self.fixture.owner_identity["ExternalId"]
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["CreateResourceUpdateForwarding"]["ParameterValue"],
            "false",
        )

    def test_manual_quickstart_only_can_add_supplementary_coverage(self):
        self.fixture.current_stack = None
        self.forwarding()
        del self.fixture.owner_identity["ExternalId"]
        request = existing_tests.event()
        request["ResourceProperties"]["InstrumentationResourceTypes"] = (
            "aws:ec2:instance,aws:eks:cluster"
        )
        self.fixture.subject.prepare(request)
        self.assertEqual(
            self.fixture.values()["SupplementaryResourceTypes"]["ParameterValue"],
            "aws:eks:cluster",
        )
        self.assertNotIn("LegacyPermissionsTemplateVersion", self.fixture.values())
        self.fixture.cf.get_template.assert_not_called()

    def test_manual_role_child_must_match_parent_and_identity(self):
        self.fixture.current_stack = None
        self.forwarding()
        del self.fixture.owner_identity["ExternalId"]
        for key in ("IAMRoleName", "DatadogSite", "ExternalId"):
            with self.subTest(key=key):
                old = self.role_parameters[key]
                self.role_parameters[key] = "another-integration"
                self.assert_rejected()
                self.role_parameters[key] = old
        self.role_parent = "another-parent"
        self.assert_rejected()

    def test_missing_nested_external_id_is_not_adopted(self):
        del self.role_parameters["ExternalId"]
        self.assert_rejected()

    def test_conflicting_root_identity_is_not_overridden_by_child(self):
        self.forwarding()
        self.fixture.owner_identity["ExternalId"] = "another-integration"
        self.assert_rejected()

    def test_integration_role_must_have_a_verifiable_owner_and_resource(self):
        self.role_name = "unrelated-role"
        self.assert_rejected()
        self.role_name = PROPS["IAMRoleName"]
        self.fixture.role_owners[PROPS["IAMRoleName"]] = None
        self.assert_rejected()

    def test_adoption_requires_unambiguous_current_role_trust(self):
        statement = self.role["AssumeRolePolicyDocument"]["Statement"][0]
        for changed in (
            {
                **statement,
                "Condition": {"StringEquals": {"sts:ExternalId": "another-org"}},
            },
            {**statement, "Principal": {"AWS": "*"}},
            {**statement, "Action": "sts:*"},
        ):
            with self.subTest(changed=changed):
                self.role["AssumeRolePolicyDocument"]["Statement"] = [changed]
                self.assert_rejected()
        self.role["AssumeRolePolicyDocument"]["Statement"] = [statement, statement]
        self.assert_rejected()

    def test_unknown_or_modified_template_is_rejected(self):
        for source in (
            legacy_template(version="v4.20.1"),
            legacy_template() + "\nMetadata: {custom: true}\n",
            legacy_template().replace(
                "ManageBasePermissions: false", "ManageBasePermissions: true"
            ),
        ):
            with self.subTest(source=source[:20]):
                self.fixture.cf.get_template.return_value = {"TemplateBody": source}
                self.assert_rejected()

    def test_legacy_child_parameters_and_parent_cannot_be_changed(self):
        for key in self.legacy_parameters:
            with self.subTest(key=key):
                old = self.legacy_parameters[key]
                self.legacy_parameters[key] = "changed"
                self.assert_rejected()
                self.legacy_parameters[key] = old
        self.legacy_parent = "another-root"
        self.assert_rejected()

    def test_empty_legacy_types_stay_empty_when_new_permissions_are_added(self):
        self.legacy_parameters["InstrumentationResourceTypes"] = ""
        previous = launcher.parameters(self.fixture.current_stack)
        previous["InstrumentationResourceTypes"] = ""
        self.fixture.current_stack["Parameters"] = stack_parameters(previous)
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["LegacyInstrumentationResourceTypes"][
                "ParameterValue"
            ],
            "",
        )
        self.assertEqual(
            self.fixture.values()["InstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance",
        )

    def test_adopted_stack_relaunch_preserves_the_original_frozen_types(self):
        self.adopted()
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["LegacyInstrumentationResourceTypes"][
                "ParameterValue"
            ],
            "aws:ec2:instance",
        )
        self.assertEqual(
            self.fixture.values()["InstrumentationResourceTypes"]["ParameterValue"],
            "aws:ec2:instance,aws:eks:cluster",
        )
        self.fixture.cf.get_template.assert_not_called()

    def test_adoption_preserves_tags_without_an_inherited_version_tag_update(self):
        tags = [{"Key": "customer", "Value": "preserve"}]
        self.fixture.current_stack["Tags"] = tags
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.cf.create_change_set.call_args.kwargs["Tags"], tags
        )

    def test_adopted_stack_uses_version_output_to_reject_downgrades(self):
        self.adopted()
        self.fixture.current_stack["Outputs"] = [
            {"OutputKey": "InstallationTemplateVersion", "OutputValue": "v4.24.0"}
        ]
        self.assert_rejected()

    def test_adopted_stack_requires_both_children_and_consistent_pins(self):
        self.adopted()
        for key, value in (
            ("LegacyPermissionsTemplateVersion", "v4.20.1"),
            ("LegacyInstrumentationResourceTypes", "aws:eks:cluster"),
            ("ExternalId", ""),
        ):
            previous = launcher.parameters(self.fixture.current_stack)
            with self.subTest(key=key):
                self.fixture.current_stack["Parameters"] = stack_parameters(
                    {**previous, key: value}
                )
                self.assert_rejected()
            self.fixture.current_stack["Parameters"] = stack_parameters(previous)
        self.owned_scope = "role"
        self.assert_rejected()
        self.owned_scope = "stack"
        self.fixture.cf.describe_stack_resources.return_value["StackResources"].pop()
        self.assert_rejected()

    def test_adoption_protection_survives_an_idempotent_retry(self):
        result = self.fixture.subject.prepare(existing_tests.event())
        description = self.fixture.cf.create_change_set.call_args.kwargs["Description"]
        self.fixture.cf.describe_change_set.side_effect = None
        self.fixture.cf.describe_change_set.return_value = {"Description": description}
        self.fixture.cf.create_change_set.reset_mock()
        retry = self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(result, retry)
        self.fixture.cf.create_change_set.assert_not_called()

    def test_discovery_denials_do_not_become_adoption(self):
        for method in (self.fixture.cf.get_template, self.fixture.iam.get_role):
            with self.subTest(method=method):
                original = method.side_effect
                method.side_effect = ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "GetTemplate",
                )
                with self.assertRaises(ClientError):
                    self.fixture.subject.prepare(existing_tests.event())
                method.side_effect = original
        self.fixture.cf.create_change_set.assert_not_called()


class LegacyChangeSetTest(unittest.TestCase):
    def setUp(self):
        self.fixture = existing_tests.PollTest()
        self.fixture.setUp()
        self.current = {**existing_tests.state(), "PreserveLegacyPermissions": True}

    def test_legacy_child_noop_and_new_permissions_child_addition_are_allowed(self):
        f = self.fixture
        f.nested_changes([])
        f.pages[existing_tests.NESTED_CHANGE_SET][0].update(
            Status="FAILED", StatusReason="No updates are to be performed."
        )
        f.pages[existing_tests.CHANGE_SET][0]["Changes"].append(
            {
                "ResourceChange": {
                    "Action": "Add",
                    "LogicalResourceId": launcher.OWNED_PERMISSIONS_RESOURCE,
                    "ResourceType": "AWS::CloudFormation::Stack",
                    "ChangeSetId": "owned-change-set",
                }
            }
        )
        f.pages["owned-change-set"] = [
            {
                "Status": "CREATE_COMPLETE",
                "IncludeNestedStacks": True,
                "Changes": [
                    {
                        "ResourceChange": {
                            "Action": "Add",
                            "LogicalResourceId": "Helper",
                            "ResourceType": "AWS::Lambda::Function",
                        }
                    }
                ],
            }
        ]
        launcher.poll_change_set(f.client, existing_tests.event(), self.current)
        f.client.execute_change_set.assert_called_once()

    def test_any_legacy_child_resource_change_blocks_execution(self):
        for action in ("Add", "Modify", "Remove"):
            with self.subTest(action=action):
                self.fixture.nested_changes(
                    [
                        {
                            "ResourceChange": {
                                "Action": action,
                                "Replacement": "False",
                                "LogicalResourceId": "LegacyHelper",
                                "ResourceType": "AWS::Lambda::Function",
                            }
                        }
                    ]
                )
                with self.assertRaisesRegex(
                    launcher.InstallationError, "frozen legacy"
                ):
                    launcher.poll_change_set(
                        self.fixture.client, existing_tests.event(), self.current
                    )
        self.fixture.client.execute_change_set.assert_not_called()

    def test_legacy_child_cannot_be_recreated_removed_or_replaced(self):
        for action, replacement in (
            ("Add", "False"),
            ("Remove", "False"),
            ("Modify", "True"),
            ("Modify", "Conditional"),
        ):
            with self.subTest(action=action, replacement=replacement):
                self.fixture.nested_changes([])
                self.fixture.pages[existing_tests.CHANGE_SET][0]["Changes"][0][
                    "ResourceChange"
                ].update(Action=action, Replacement=replacement)
                with self.assertRaises(launcher.InstallationError):
                    launcher.poll_change_set(
                        self.fixture.client, existing_tests.event(), self.current
                    )
        self.fixture.client.execute_change_set.assert_not_called()

    def test_legacy_changes_on_later_pages_cannot_be_missed(self):
        self.fixture.nested_changes([])
        self.fixture.pages[existing_tests.NESTED_CHANGE_SET].append(
            {
                "Status": "CREATE_COMPLETE",
                "IncludeNestedStacks": True,
                "Changes": [
                    {
                        "ResourceChange": {
                            "Action": "Modify",
                            "Replacement": "False",
                            "LogicalResourceId": "LegacyHelper",
                            "ResourceType": "AWS::Lambda::Function",
                        }
                    }
                ],
            }
        )
        with self.assertRaisesRegex(launcher.InstallationError, "frozen legacy"):
            launcher.poll_change_set(
                self.fixture.client, existing_tests.event(), self.current
            )
        self.fixture.client.execute_change_set.assert_not_called()


class LegacyTemplateTest(unittest.TestCase):
    def test_known_template_hashes_cover_yaml_and_json_in_both_partitions(self):
        for prefix in (
            "https://test-bucket.s3.amazonaws.com",
            "https://test-gov.s3.us-gov-west-1.amazonaws.com",
        ):
            with (
                self.subTest(prefix=prefix),
                patch.dict(
                    os.environ,
                    {
                        "INSTALLATION_TEMPLATE_URL": prefix
                        + "/aws/v4.23.0/main_agent_installation.yaml"
                    },
                ),
            ):
                source = legacy_template(prefix=prefix)
                parsed = json.loads(
                    json.dumps(yaml.load(source, Loader=TemplateLoader), default=str)
                )
                for body in (
                    source,
                    source.replace("\n", "\r\n"),
                    parsed,
                    json.dumps(parsed),
                ):
                    self.assertTrue(launcher.is_known_legacy_template(body))
                self.assertFalse(
                    launcher.is_known_legacy_template(
                        {**parsed, "Metadata": {"changed": True}}
                    )
                )
                self.assertFalse(
                    launcher.is_known_legacy_template(
                        legacy_template(prefix="https://untrusted.invalid")
                    )
                )

    def test_legacy_child_is_pinned_and_keeps_normal_deletion_behavior(self):
        current = yaml.load(
            Path(__file__).with_name("main_agent_installation.yaml").read_text(),
            Loader=TemplateLoader,
        )
        original = yaml.load(legacy_template(), Loader=TemplateLoader)
        params = {
            **PROPS,
            "LegacyPermissionsTemplateVersion": "v4.21.1",
            "LegacyInstrumentationResourceTypes": "aws:ec2:instance",
            "InstrumentationResourceTypes": ["aws:ec2:instance", "aws:eks:cluster"],
            "InstrumentationRefreshToken": "new-request",
        }
        no_value = object()

        def evaluate(value):
            if isinstance(value, list):
                return [evaluate(v) for v in value]
            if not isinstance(value, dict):
                return value
            if "Ref" in value:
                return (
                    no_value if value["Ref"] == "AWS::NoValue" else params[value["Ref"]]
                )
            if "Fn::If" in value:
                condition, yes, no = value["Fn::If"]
                self.assertEqual(condition, "HasLegacyPermissions")
                return evaluate(
                    yes if params["LegacyPermissionsTemplateVersion"] else no
                )
            if "Fn::Sub" in value:
                return value["Fn::Sub"].replace(
                    "${LegacyPermissionsTemplateVersion}",
                    params["LegacyPermissionsTemplateVersion"],
                )
            if "Fn::Join" in value:
                separator, values = value["Fn::Join"]
                return separator.join(evaluate(values))
            resolved = {key: evaluate(item) for key, item in value.items()}
            return {key: item for key, item in resolved.items() if item is not no_value}

        frozen = current["Resources"][launcher.PERMISSIONS_RESOURCE]
        resolved = evaluate(frozen["Properties"])
        resolved["TemplateURL"] = resolved["TemplateURL"].replace(
            "<BUCKET_PLACEHOLDER>", "test-bucket"
        )
        # The old root's requested types are restored by CloudFormation on rollback.
        params["InstrumentationResourceTypes"] = ["aws:ec2:instance"]
        self.assertEqual(
            resolved,
            evaluate(
                original["Resources"][launcher.PERMISSIONS_RESOURCE]["Properties"]
            ),
        )
        for resource in current["Resources"].values():
            self.assertNotIn("DeletionPolicy", resource)
            self.assertNotIn("UpdateReplacePolicy", resource)
        new = current["Resources"][launcher.OWNED_PERMISSIONS_RESOURCE]
        self.assertEqual(new["Condition"], "HasLegacyPermissions")
        self.assertEqual(new["DependsOn"], launcher.PERMISSIONS_RESOURCE)
        self.assertEqual(
            new["Properties"]["Parameters"]["InstrumentationPolicyScope"], "stack"
        )
        params["LegacyPermissionsTemplateVersion"] = ""
        fresh = evaluate(frozen["Properties"])
        self.assertEqual(fresh["Parameters"]["InstrumentationPolicyScope"], "stack")
        self.assertEqual(
            fresh["Parameters"]["InstrumentationRefreshToken"], "new-request"
        )

    def test_identity_read_permission_is_limited_to_the_selected_role(self):
        template = yaml.load(
            Path(__file__)
            .with_name("main_agent_installation_launcher.yaml")
            .read_text(),
            Loader=TemplateLoader,
        )
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        identity = next(
            statement
            for statement in statements
            if statement["Sid"] == "IntegrationRoleIdentity"
        )
        self.assertEqual(identity["Action"], "iam:GetRole")
        self.assertEqual(
            identity["Resource"],
            {
                "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${IAMRoleName}"
            },
        )


if __name__ == "__main__":
    unittest.main()
