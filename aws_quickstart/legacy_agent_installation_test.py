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
        Path(__file__).parent
        / f"fixtures/main_agent_installation_{version.replace('.', '_')}.yaml"
    ).read_text()
    return source.replace(
        "https://<BUCKET_PLACEHOLDER>.s3.amazonaws.com", prefix
    ).replace("<VERSION_PLACEHOLDER>", version)


def stack_parameters(values):
    return [
        {"ParameterKey": key, "ParameterValue": value} for key, value in values.items()
    ]


class LegacyAdoptionTest(unittest.TestCase):
    legacy_version = "v4.21.1"
    legacy_origin = "https://test-bucket.s3.amazonaws.com"

    def setUp(self):
        self.fixture = existing_tests.LauncherTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        previous = {key: value for key, value in PROPS.items() if key != "ExternalId"}
        if self.legacy_version == "v4.21.1":
            previous["APPKey"] = "legacy-app-secret"
        f.existing(Parameters=stack_parameters(previous))
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
        self.permissions_url = f"{self.legacy_origin}/aws/{self.legacy_version}/datadog_integration_permissions.yaml"
        self.child_event = {
            "LogicalResourceId": launcher.PERMISSIONS_RESOURCE,
            "PhysicalResourceId": "permissions",
            "ResourceType": "AWS::CloudFormation::Stack",
            "ResourceStatus": "CREATE_COMPLETE",
            "ResourceProperties": json.dumps({"TemplateURL": self.permissions_url}),
        }
        f.cf.describe_stack_events.side_effect = None
        f.cf.describe_stack_events.return_value = {"StackEvents": [self.child_event]}
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
        f.cf.get_template.return_value = {
            "TemplateBody": legacy_template(
                prefix=self.legacy_origin, version=self.legacy_version
            )
        }
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
        return self.fixture.describe_stack_resources(
            StackName=StackName, LogicalResourceId=LogicalResourceId
        )

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

    def adopted(self, *, pinned=True):
        previous = launcher.parameters(self.fixture.current_stack)
        previous.pop("APPKey", None)
        previous.update(
            ExternalId=PROPS["ExternalId"],
            LegacyPermissionsTemplateVersion=self.legacy_version,
            LegacyPermissionsTemplateURL=self.permissions_url,
            LegacyInstrumentationResourceTypes="aws:ec2:instance",
            InstrumentationResourceTypes="aws:ec2:instance,aws:eks:cluster",
        )
        if not pinned:
            previous.pop("LegacyPermissionsTemplateURL")
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
            values["LegacyPermissionsTemplateURL"]["ParameterValue"],
            self.permissions_url,
        )
        self.assertEqual(
            values["LegacyPermissionsTemplateVersion"]["ParameterValue"],
            self.legacy_version,
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
        original = legacy_template(
            prefix=self.legacy_origin, version=self.legacy_version
        )
        for source in (
            original.replace(self.legacy_version, "v4.20.1"),
            original + "\nMetadata: {custom: true}\n",
            original.replace(
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
        self.assertEqual(
            self.fixture.values()["LegacyPermissionsTemplateVersion"]["ParameterValue"],
            self.legacy_version,
        )
        self.assertEqual(
            self.fixture.values()["LegacyPermissionsTemplateURL"]["ParameterValue"],
            self.permissions_url,
        )
        self.fixture.cf.get_template.assert_not_called()
        self.fixture.cf.describe_stack_events.assert_not_called()

    def test_older_adopted_stack_recovers_exact_url_from_paginated_physical_history(
        self,
    ):
        self.adopted()
        previous = launcher.parameters(self.fixture.current_stack)
        previous.pop("LegacyPermissionsTemplateURL")
        self.fixture.current_stack["Parameters"] = stack_parameters(previous)
        self.fixture.cf.describe_stack_events.side_effect = [
            {
                "StackEvents": [
                    {**self.child_event, "PhysicalResourceId": "another-child"}
                ],
                "NextToken": "page-2",
            },
            {"StackEvents": [self.child_event]},
        ]
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["LegacyPermissionsTemplateURL"]["ParameterValue"],
            self.permissions_url,
        )
        self.fixture.cf.describe_stack_events.assert_any_call(
            StackName=ROOT, NextToken="page-2"
        )

    def test_unapproved_url_pin_is_rejected(self):
        self.adopted()
        previous = launcher.parameters(self.fixture.current_stack)
        for url in (
            self.permissions_url.replace("https://", "http://"),
            self.permissions_url + "?versionId=unverified",
            self.permissions_url.replace(self.legacy_version, "v4.20.1"),
            self.permissions_url.replace(self.legacy_origin, "https://unknown.invalid"),
        ):
            with self.subTest(url=url):
                self.fixture.current_stack["Parameters"] = stack_parameters(
                    {**previous, "LegacyPermissionsTemplateURL": url}
                )
                self.assert_rejected()

    def test_missing_or_untrusted_child_history_does_not_guess_an_origin(self):
        self.adopted(pinned=False)
        for change in (
            {"ResourceProperties": "{}"},
            {"ResourceProperties": "not-json"},
            {"ResourceProperties": "[]"},
            {
                "ResourceProperties": json.dumps(
                    {"TemplateURL": "https://unknown.invalid/old.yaml"}
                )
            },
            {"PhysicalResourceId": "replaced-child"},
            {"ResourceStatus": "UPDATE_FAILED"},
        ):
            with self.subTest(change=change):
                self.fixture.cf.describe_stack_events.return_value = {
                    "StackEvents": [{**self.child_event, **change}]
                }
                self.assert_rejected()
        self.fixture.cf.describe_stack_events.return_value = {
            "StackEvents": [
                {**self.child_event, "ResourceProperties": "{}"},
                self.child_event,
            ]
        }
        self.assert_rejected()

    def test_history_lookup_is_bounded(self):
        self.adopted(pinned=False)
        self.fixture.cf.describe_stack_events.return_value = {
            "StackEvents": [],
            "NextToken": "more",
        }
        self.assert_rejected()
        self.assertEqual(self.fixture.cf.describe_stack_events.call_count, 20)

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


class Legacy422AdoptionTest(LegacyAdoptionTest):
    legacy_version = "v4.22.0"

    def test_api_key_only_fixture_and_previous_parameters_do_not_require_app_key(self):
        template = yaml.load(
            legacy_template(prefix=self.legacy_origin, version=self.legacy_version),
            Loader=TemplateLoader,
        )
        self.assertNotIn("APPKey", template["Parameters"])
        self.assertNotIn("APPKey", launcher.parameters(self.fixture.current_stack))
        self.fixture.subject.prepare(existing_tests.event())
        self.assertEqual(
            self.fixture.values()["APIKey"],
            {"ParameterKey": "APIKey", "UsePreviousValue": True},
        )
        self.assertNotIn("APPKey", self.fixture.values())


class PublishedLegacyAdoptionTest(LegacyAdoptionTest):
    legacy_origin = (
        "https://datadog-cloudformation-template-quickstart.s3.amazonaws.com"
    )


class PublishedLegacy422AdoptionTest(Legacy422AdoptionTest):
    legacy_origin = (
        "https://datadog-cloudformation-template-quickstart.s3.amazonaws.com"
    )


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

    def test_frozen_url_retargeting_or_missing_context_blocks_execution(self):
        f = self.fixture
        for context in (
            None,
            "not-json",
            json.dumps(
                {
                    "Properties": {
                        "TemplateURL": launcher.PUBLISHED_TEMPLATE_ORIGIN
                        + "/aws/v4.21.1/datadog_integration_permissions.yaml"
                    }
                }
            ),
        ):
            with self.subTest(context=context):
                f.nested_changes([])
                f.pages[existing_tests.CHANGE_SET][0]["Changes"][0]["ResourceChange"][
                    "AfterContext"
                ] = context
                with self.assertRaisesRegex(launcher.InstallationError, "template URL"):
                    launcher.poll_change_set(
                        f.client, existing_tests.event(), self.current
                    )
                f.client.execute_change_set.assert_not_called()
        f.change_paginator.paginate.assert_any_call(
            ChangeSetName=existing_tests.CHANGE_SET, IncludePropertyValues=True
        )

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
        for version in ("v4.21.1", "v4.22.0"):
            for prefix in (
                "https://test-bucket.s3.amazonaws.com",
                "https://test-gov.s3.us-gov-west-1.amazonaws.com",
            ):
                with (
                    self.subTest(version=version, prefix=prefix),
                    patch.dict(
                        os.environ,
                        {
                            "INSTALLATION_TEMPLATE_URL": prefix
                            + "/aws/v4.23.0/main_agent_installation.yaml"
                        },
                    ),
                ):
                    source = legacy_template(prefix=prefix, version=version)
                    parsed = json.loads(
                        json.dumps(
                            yaml.load(source, Loader=TemplateLoader), default=str
                        )
                    )
                    for body in (
                        source,
                        source.replace("\n", "\r\n"),
                        parsed,
                        json.dumps(parsed),
                    ):
                        self.assertEqual(
                            launcher.legacy_template_version(body), version
                        )
                        self.assertTrue(launcher.is_known_legacy_template(body))
                    self.assertIsNone(
                        launcher.legacy_template_version(
                            {**parsed, "Metadata": {"changed": True}}
                        )
                    )
                    self.assertFalse(
                        launcher.is_known_legacy_template(
                            legacy_template(
                                prefix="https://untrusted.invalid", version=version
                            )
                        )
                    )

    def test_published_release_formats_keep_their_url_with_a_private_candidate(self):
        with patch.dict(
            os.environ,
            {
                "INSTALLATION_TEMPLATE_URL": "https://test-bucket.s3.amazonaws.com/aws/v4.23.0/main_agent_installation.yaml"
            },
        ):
            for version in ("v4.21.1", "v4.22.0"):
                source = legacy_template(
                    prefix=launcher.PUBLISHED_TEMPLATE_ORIGIN, version=version
                )
                parsed = json.loads(
                    json.dumps(yaml.load(source, Loader=TemplateLoader), default=str)
                )
                for body in (
                    source,
                    source.replace("\n", "\r\n"),
                    parsed,
                    json.dumps(parsed),
                ):
                    self.assertEqual(
                        launcher.legacy_template_configuration(body),
                        {
                            "version": version,
                            "permissions_url": f"{launcher.PUBLISHED_TEMPLATE_ORIGIN}/aws/{version}/datadog_integration_permissions.yaml",
                        },
                    )

    def test_published_asset_permissions_are_exact_and_delegated(self):
        template = yaml.load(
            Path(__file__)
            .with_name("main_agent_installation_launcher.yaml")
            .read_text(),
            Loader=TemplateLoader,
        )
        statements = template["Resources"]["LauncherRole"]["Properties"]["Policies"][0][
            "PolicyDocument"
        ]["Statement"]
        objects = {
            f"aws/{version}/{name}"
            for version in ("v4.21.1", "v4.22.0")
            for name in (
                "datadog_integration_permissions.yaml",
                "datadog_agent_resource_update_forwarding.yaml",
            )
        }
        reads = next(s for s in statements if s["Sid"] == "VersionedTemplates")
        actual = {
            r["Fn::Sub"]
            for r in reads["Resource"]
            if "datadog-cloudformation-template-quickstart" in r["Fn::Sub"]
        }
        self.assertEqual(
            actual,
            {
                "arn:${AWS::Partition}:s3:::datadog-cloudformation-template-quickstart/"
                + p
                for p in objects
            },
        )
        self.assertEqual(reads["Action"], "s3:GetObject")
        writes = next(s for s in statements if s["Sid"] == "NestedStackTemplates")
        self.assertIn(
            "cloudformation.amazonaws.com",
            writes["Condition"]["ForAnyValue:StringEquals"]["aws:CalledVia"],
        )
        urls = {
            url
            for url in writes["Condition"]["StringLike"]["cloudformation:TemplateUrl"]
            if url.startswith(launcher.PUBLISHED_TEMPLATE_ORIGIN)
        }
        self.assertEqual(
            urls, {launcher.PUBLISHED_TEMPLATE_ORIGIN + "/" + p for p in objects}
        )

    def test_origin_mixing_and_pre_normalized_urls_are_rejected(self):
        with patch.dict(
            os.environ,
            {
                "INSTALLATION_TEMPLATE_URL": "https://test-bucket.s3.amazonaws.com/aws/v4.23.0/main_agent_installation.yaml"
            },
        ):
            for version in ("v4.21.1", "v4.22.0"):
                source = legacy_template(
                    prefix=launcher.PUBLISHED_TEMPLATE_ORIGIN, version=version
                )
                for altered in (
                    source.replace(
                        launcher.PUBLISHED_TEMPLATE_ORIGIN,
                        "https://test-bucket.s3.amazonaws.com",
                        1,
                    ),
                    source.replace(
                        launcher.PUBLISHED_TEMPLATE_ORIGIN, "https://unknown.invalid"
                    ),
                    source.replace(
                        f"{launcher.PUBLISHED_TEMPLATE_ORIGIN}/aws/{version}/",
                        "https://legacy-template.invalid/aws/release/",
                    ).replace(f"# version: {version}", "# version: legacy"),
                ):
                    parsed = json.loads(
                        json.dumps(
                            yaml.load(altered, Loader=TemplateLoader), default=str
                        )
                    )
                    for body in (altered, parsed, json.dumps(parsed)):
                        self.assertIsNone(launcher.legacy_template_configuration(body))

    def test_release_labels_and_dependencies_cannot_be_mixed(self):
        with patch.dict(
            os.environ,
            {
                "INSTALLATION_TEMPLATE_URL": "https://test-bucket.s3.amazonaws.com/aws/v4.23.0/main_agent_installation.yaml"
            },
        ):
            for version, other in (("v4.21.1", "v4.22.0"), ("v4.22.0", "v4.21.1")):
                source = legacy_template(version=version)
                for altered in (
                    source.replace(version, other),
                    source.replace(version, "v4.22.1"),
                    source.replace(
                        version + "/datadog_integration_permissions.yaml",
                        other + "/datadog_integration_permissions.yaml",
                    ),
                ):
                    parsed = json.loads(
                        json.dumps(
                            yaml.load(altered, Loader=TemplateLoader), default=str
                        )
                    )
                    for body in (altered, parsed, json.dumps(parsed)):
                        with self.subTest(
                            version=version, body_type=type(body).__name__
                        ):
                            self.assertIsNone(launcher.legacy_template_version(body))

    def test_legacy_child_is_pinned_and_keeps_normal_deletion_behavior(self):
        current = yaml.load(
            Path(__file__).with_name("main_agent_installation.yaml").read_text(),
            Loader=TemplateLoader,
        )
        self.assertEqual(
            set(
                current["Parameters"]["LegacyPermissionsTemplateVersion"][
                    "AllowedValues"
                ]
            ),
            {"", "v4.21.1", "v4.22.0"},
        )
        params = {
            **PROPS,
            "LegacyPermissionsTemplateVersion": "v4.21.1",
            "LegacyPermissionsTemplateURL": "",
            "LegacyInstrumentationResourceTypes": "aws:ec2:instance",
            "InstrumentationResourceTypes": ["aws:ec2:instance", "aws:eks:cluster"],
            "InstrumentationRefreshToken": "new-request",
        }
        no_value = object()

        def evaluate(value):
            if isinstance(value, list):
                return [evaluate(v) for v in value]
            if not isinstance(value, dict):
                return (
                    value.replace("<BUCKET_PLACEHOLDER>", "test-bucket")
                    if isinstance(value, str)
                    else value
                )
            if "Fn::Equals" in value:
                left, right = evaluate(value["Fn::Equals"])
                return left == right
            if "Fn::Contains" in value:
                collection, item = evaluate(value["Fn::Contains"])
                return item in collection
            if "Fn::And" in value:
                return all(evaluate(value["Fn::And"]))
            if "Fn::Or" in value:
                return any(evaluate(value["Fn::Or"]))
            if "Fn::Not" in value:
                return not evaluate(value["Fn::Not"])[0]
            if "Ref" in value:
                return (
                    no_value if value["Ref"] == "AWS::NoValue" else params[value["Ref"]]
                )
            if "Fn::If" in value:
                condition, yes, no = value["Fn::If"]
                enabled = evaluate(current["Conditions"][condition])
                return evaluate(yes if enabled else no)
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
        for version in ("v4.21.1", "v4.22.0"):
            for origin in (
                "https://test-bucket.s3.amazonaws.com",
                launcher.PUBLISHED_TEMPLATE_ORIGIN,
            ):
                with self.subTest(version=version, origin=origin):
                    params["LegacyPermissionsTemplateVersion"] = version
                    params["LegacyPermissionsTemplateURL"] = (
                        f"{origin}/aws/{version}/datadog_integration_permissions.yaml"
                    )
                    params["InstrumentationResourceTypes"] = [
                        "aws:ec2:instance",
                        "aws:eks:cluster",
                    ]
                    original = yaml.load(
                        legacy_template(prefix=origin, version=version),
                        Loader=TemplateLoader,
                    )
                    resolved = evaluate(frozen["Properties"])
                    # The old root's requested types are restored by CloudFormation on rollback.
                    params["InstrumentationResourceTypes"] = ["aws:ec2:instance"]
                    self.assertEqual(
                        resolved,
                        evaluate(
                            original["Resources"][launcher.PERMISSIONS_RESOURCE][
                                "Properties"
                            ]
                        ),
                    )
        rule = current["Rules"]["LegacyTemplateURLMatchesVersion"]
        allowed_urls = evaluate(
            current["Parameters"]["LegacyPermissionsTemplateURL"]["AllowedValues"]
        )
        for version in ("", "v4.21.1", "v4.22.0"):
            params["LegacyPermissionsTemplateVersion"] = version
            for url in [*allowed_urls, "https://untrusted.invalid/template.yaml"]:
                params["LegacyPermissionsTemplateURL"] = url
                accepted = url in allowed_urls and (
                    not evaluate(rule["RuleCondition"])
                    or all(evaluate(a["Assert"]) for a in rule["Assertions"])
                )
                self.assertEqual(
                    accepted,
                    (not url and not version)
                    or (
                        bool(version)
                        and f"/aws/{version}/" in url
                        and url in allowed_urls
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
        params["LegacyPermissionsTemplateURL"] = ""
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
