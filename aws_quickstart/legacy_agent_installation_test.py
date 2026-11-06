import json
import unittest
from pathlib import Path

import yaml

import launch_agent_installation_test as existing_tests

launcher = existing_tests.launcher


class TemplateLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, tag, node):
    value = getattr(loader, f"construct_{node.id}")(node)
    return {tag if tag in {"Ref", "Condition"} else f"Fn::{tag}": value}


TemplateLoader.add_multi_constructor("!", intrinsic)


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
