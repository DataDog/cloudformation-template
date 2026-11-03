import copy
import unittest
from importlib import import_module
from unittest.mock import Mock, patch

from attach_integration_permissions_test import make_iam_mock

permissions = import_module("attach_integration_permissions")

ACCOUNT = "123456789012"
ROLE = "DatadogIntegrationRole"
OWNER = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/installation-permissions/one"
OTHER_OWNER = f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/other-permissions/two"
DOCUMENT = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow", "Action": "ec2:DescribeInstances", "Resource": "*"}
    ],
}


class TestInstrumentationPolicyOwnership(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()
        self.policies = {}
        self.attached = set()
        self.cf = Mock()
        self.cf.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]
        }
        self.enterContext(
            patch.object(
                permissions.boto3,
                "client",
                side_effect=lambda service, **kwargs: (
                    self.cf if service == "cloudformation" else self.iam
                ),
            )
        )
        self.response = self.enterContext(
            patch.object(permissions, "send_cfn_response")
        )
        self.fetch = self.enterContext(
            patch.object(
                permissions,
                "_fetch_instrumentation_permissions_for_resource_types",
                return_value=([DOCUMENT], []),
            )
        )
        self.enterContext(patch.object(permissions, "manage_permissions_boundaries"))
        self.enterContext(
            patch.object(permissions, "cleanup_permissions_boundaries", return_value=[])
        )
        self.iam.list_attached_role_policies.side_effect = lambda **kwargs: {
            "AttachedPolicies": [
                {"PolicyName": self.policies[arn]["PolicyName"], "PolicyArn": arn}
                for arn in sorted(self.attached)
            ]
        }
        self.iam.get_account_summary.return_value = {
            "SummaryMap": {"AttachedPoliciesPerRoleQuota": 10}
        }
        self.iam.get_policy.side_effect = self.get_policy
        self.iam.list_policy_tags.side_effect = lambda **kwargs: {
            "Tags": self.policies[kwargs["PolicyArn"]]["Tags"],
            "IsTruncated": False,
        }
        self.iam.create_policy.side_effect = self.create_policy
        self.iam.attach_role_policy.side_effect = lambda **kwargs: self.attached.add(
            kwargs["PolicyArn"]
        )
        self.iam.detach_role_policy.side_effect = lambda **kwargs: (
            self.attached.discard(kwargs["PolicyArn"])
        )
        self.iam.delete_policy.side_effect = self.delete_policy
        self.legacy = self.seed(None, "a")
        self.other = self.seed(OTHER_OWNER, "a")

    def seed(self, owner, generation, attached=True):
        name = permissions._instrumentation_policy_name(ROLE, 1, generation, owner)
        arn = f"arn:aws:iam::{ACCOUNT}:policy/{name}"
        self.policies[arn] = {
            "PolicyName": name,
            "PolicyDocument": copy.deepcopy(DOCUMENT),
            "Tags": [
                {"Key": permissions.INSTRUMENTATION_POLICY_OWNER_TAG, "Value": owner}
            ]
            if owner
            else [],
        }
        if attached:
            self.attached.add(arn)
        return arn

    def get_policy(self, *, PolicyArn):
        if PolicyArn not in self.policies:
            raise self.iam.exceptions.NoSuchEntityException()
        return {"Policy": {"Arn": PolicyArn}}

    def create_policy(self, **kwargs):
        arn = f"arn:aws:iam::{ACCOUNT}:policy/{kwargs['PolicyName']}"
        if arn in self.policies:
            raise RuntimeError("Policy already exists")
        self.policies[arn] = dict(kwargs)
        return {"Policy": {"Arn": arn}}

    def delete_policy(self, *, PolicyArn):
        if PolicyArn in self.attached:
            raise self.iam.exceptions.DeleteConflictException()
        self.policies.pop(PolicyArn, None)

    def event(self, kind="Create", scope="stack", types="aws:ec2:instance"):
        return {
            "RequestType": kind,
            "StackId": OWNER,
            "LogicalResourceId": "Permissions",
            "ResourceProperties": {
                "DatadogIntegrationRole": ROLE,
                "AccountId": ACCOUNT,
                "ManageBasePermissions": "false",
                "ResourceCollectionPermissions": "false",
                "InstrumentationPolicyScope": scope,
                "InstrumentationResourceTypes": types,
                "FailOnInstrumentationError": "true",
                "DatadogSite": "datadoghq.com",
            },
        }

    def assert_foreign_policies_preserved(self):
        self.assertTrue({self.legacy, self.other} <= self.attached)
        self.assertTrue({self.legacy, self.other} <= self.policies.keys())
        for call in (
            self.iam.detach_role_policy.call_args_list
            + self.iam.delete_policy.call_args_list
        ):
            self.assertNotIn(call.kwargs["PolicyArn"], {self.legacy, self.other})

    def assert_response(self, status):
        self.assertEqual(self.response.call_args.args[3], status)

    def test_create_preserves_quickstart_and_other_addon_grants(self):
        previous = copy.deepcopy(self.policies)
        permissions.handle_create_update(self.event(), None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assert_foreign_policies_preserved()
        for arn, policy in previous.items():
            self.assertEqual(self.policies[arn], policy)
        created = self.iam.create_policy.call_args.kwargs
        self.assertEqual(
            created["Tags"],
            [{"Key": permissions.INSTRUMENTATION_POLICY_OWNER_TAG, "Value": OWNER}],
        )

    def test_update_replaces_only_this_stack_generation(self):
        old = self.seed(OWNER, "a")
        request = self.event("Update")
        request["OldResourceProperties"] = {
            **request["ResourceProperties"],
            "InstrumentationRefreshToken": "previous",
        }
        request["ResourceProperties"]["InstrumentationRefreshToken"] = "current"
        permissions.handle_create_update(request, None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assertNotIn(old, self.policies)
        self.assert_foreign_policies_preserved()

    def test_failed_first_create_and_delete_leave_existing_grants_untouched(self):
        self.fetch.side_effect = RuntimeError("unavailable")
        permissions.handle_create_update(self.event(), None)
        self.assert_response(permissions.cfnresponse.FAILED)
        permissions.handle_delete(self.event("Delete"), None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assert_foreign_policies_preserved()
        self.iam.create_policy.assert_not_called()

    def test_partial_creation_and_rollback_delete_preserve_other_owners(self):
        self.fetch.return_value = ([DOCUMENT, DOCUMENT], [])
        calls = 0

        def create(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("creation failed")
            return self.create_policy(**kwargs)

        self.iam.create_policy.side_effect = create
        permissions.handle_create_update(self.event(), None)
        self.assert_response(permissions.cfnresponse.FAILED)
        permissions.handle_delete(self.event("Delete"), None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assert_foreign_policies_preserved()
        self.assertEqual(set(self.policies), {self.legacy, self.other})

    def test_failed_update_restores_own_grants_without_touching_other_owners(self):
        old = self.seed(OWNER, "a")

        def attach(**kwargs):
            if kwargs["PolicyArn"] != old:
                raise RuntimeError("attachment failed")
            self.attached.add(old)

        self.iam.attach_role_policy.side_effect = attach
        request = self.event("Update")
        request["OldResourceProperties"] = {
            **request["ResourceProperties"],
            "InstrumentationRefreshToken": "old",
        }
        request["ResourceProperties"]["InstrumentationRefreshToken"] = "new"
        permissions.handle_create_update(request, None)
        self.assert_response(permissions.cfnresponse.FAILED)
        self.assertIn(old, self.attached)
        self.assert_foreign_policies_preserved()

    def test_delete_releases_only_this_stack_policies(self):
        owned = self.seed(OWNER, "a")
        permissions.handle_delete(self.event("Delete"), None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assertNotIn(owned, self.policies)
        self.assert_foreign_policies_preserved()

    def test_legacy_quickstart_cleanup_cannot_match_scoped_policy_names(self):
        owned = self.seed(OWNER, "a")
        permissions.cleanup_instrumentation_policies(self.iam, ROLE, ACCOUNT, "aws")
        self.assertIn(owned, self.attached)
        self.assertIn(self.other, self.attached)
        self.assertNotIn(self.legacy, self.attached)

    def test_quota_counts_other_owners_and_fails_before_mutation(self):
        self.iam.get_account_summary.return_value = {
            "SummaryMap": {"AttachedPoliciesPerRoleQuota": 2}
        }
        permissions.handle_create_update(self.event(), None)
        self.assert_response(permissions.cfnresponse.FAILED)
        self.assert_foreign_policies_preserved()
        self.iam.create_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.assertIn(
            "managed-policy attachment capacity",
            self.response.call_args.kwargs["reason"],
        )

    def test_wrong_or_missing_owner_tags_prevent_deletion(self):
        owned = self.seed(OWNER, "a")
        for tags in (
            [],
            [
                {
                    "Key": permissions.INSTRUMENTATION_POLICY_OWNER_TAG,
                    "Value": OTHER_OWNER,
                }
            ],
        ):
            with self.subTest(tags=tags):
                self.policies[owned]["Tags"] = tags
                permissions.handle_delete(self.event("Delete"), None)
                self.assert_response(permissions.cfnresponse.FAILED)
                self.assertIn(owned, self.attached)
        self.assert_foreign_policies_preserved()
        self.iam.detach_role_policy.assert_not_called()

    def test_owner_read_denial_is_not_absence(self):
        self.iam.get_policy.side_effect = RuntimeError("access denied")
        permissions.handle_delete(self.event("Delete"), None)
        self.assert_response(permissions.cfnresponse.FAILED)
        self.iam.detach_role_policy.assert_not_called()

    def test_policy_names_fit_iam_limits_and_differ_by_stack_and_role(self):
        name = permissions._instrumentation_policy_name("r" * 64, 10, "b", OWNER)
        self.assertIsNotNone(permissions.IAM_POLICY_NAME_PATTERN.fullmatch(name))
        self.assertFalse(
            name.startswith(permissions.BASE_POLICY_PREFIX_INSTRUMENTATION + "-")
        )
        self.assertNotEqual(
            name,
            permissions._instrumentation_policy_name("r" * 64, 10, "b", OTHER_OWNER),
        )
        self.assertNotEqual(
            name, permissions._instrumentation_policy_name("s" * 64, 10, "b", OWNER)
        )

    def test_scope_changes_fail_before_mutations_and_can_roll_back(self):
        for old, new in (("role", "stack"), ("stack", "role")):
            with self.subTest(old=old, new=new):
                request = self.event("Update", scope=new)
                request["OldResourceProperties"] = self.event(scope=old)[
                    "ResourceProperties"
                ]
                self.cf.describe_stacks.return_value = {
                    "Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]
                }
                permissions.handle_create_update(request, None)
                self.assert_response(permissions.cfnresponse.FAILED)
                request["ResourceProperties"], request["OldResourceProperties"] = (
                    request["OldResourceProperties"],
                    request["ResourceProperties"],
                )
                self.cf.describe_stacks.return_value = {
                    "Stacks": [{"StackStatus": "UPDATE_ROLLBACK_IN_PROGRESS"}]
                }
                permissions.handle_create_update(request, None)
                self.assert_response(permissions.cfnresponse.SUCCESS)
        self.fetch.assert_not_called()
        self.assert_foreign_policies_preserved()
        self.iam.create_policy.assert_not_called()

    def test_empty_legacy_stack_delete_cannot_remove_quickstart_grants(self):
        permissions.handle_delete(self.event("Delete", scope="role", types=""), None)
        self.assert_response(permissions.cfnresponse.SUCCESS)
        self.assert_foreign_policies_preserved()
        self.iam.detach_role_policy.assert_not_called()

    def test_default_scope_does_not_refresh_initial_quickstart(self):
        request = self.event("Update", scope="role")
        request["OldResourceProperties"] = dict(request["ResourceProperties"])
        del request["OldResourceProperties"]["InstrumentationPolicyScope"]
        self.assertFalse(
            permissions._should_update_instrumentation_permissions(request)
        )


class TestScopedBoundaryOwnership(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()
        self.boundary = {
            "policy_name": "datadog-instrumenter-ec2-ssm-boundary",
            "policy_path": permissions.BOUNDARY_POLICY_PATH,
            "policy_arn": f"arn:aws:iam::{ACCOUNT}:policy/datadog/instrumenter-boundaries/datadog-instrumenter-ec2-ssm-boundary",
            "policy_document": DOCUMENT,
        }
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
        self.iam.get_policy.side_effect = None
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": DOCUMENT}
        }
        self.iam.list_policy_versions.return_value = {"Versions": []}
        self.iam.list_policy_tags.return_value = {"Tags": [], "IsTruncated": False}

    def tags(self, tags):
        self.iam.list_policy_tags.return_value = {"Tags": tags, "IsTruncated": False}

    def test_failed_create_cleanup_cannot_claim_ownerless_boundary(self):
        self.tags(
            [{"Key": permissions.BOUNDARY_POLICY_MANAGED_TAG_KEY, "Value": "true"}]
        )
        self.assertFalse(
            permissions._delete_permissions_boundary_policy(
                self.iam, self.boundary, OWNER, strict_ownership=True
            )
        )
        self.iam.tag_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    def test_matching_unverified_boundary_is_used_without_claiming_it(self):
        permissions._ensure_permissions_boundary_policy(
            self.iam, self.boundary, OWNER, strict_ownership=True
        )
        self.iam.tag_policy.assert_not_called()
        self.iam.create_policy_version.assert_not_called()

    def test_mismatched_unverified_or_shared_boundary_is_not_rewritten(self):
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
        }
        for tags in (
            [],
            permissions._boundary_policy_tags(OTHER_OWNER),
            permissions._boundary_policy_tags(OWNER)
            + [permissions._boundary_policy_owner_tag(OTHER_OWNER)],
        ):
            with self.subTest(tags=tags):
                self.tags(tags)
                with self.assertRaisesRegex(
                    RuntimeError, "reconciliation by its owner"
                ):
                    permissions._ensure_permissions_boundary_policy(
                        self.iam, self.boundary, OWNER, strict_ownership=True
                    )
        self.iam.tag_policy.assert_not_called()
        self.iam.create_policy_version.assert_not_called()
        self.iam.delete_policy_version.assert_not_called()

    def test_matching_managed_shared_boundary_records_a_separate_owner(self):
        self.tags(permissions._boundary_policy_tags(OTHER_OWNER))
        permissions._ensure_permissions_boundary_policy(
            self.iam, self.boundary, OWNER, strict_ownership=True
        )
        self.iam.tag_policy.assert_called_once_with(
            PolicyArn=self.boundary["policy_arn"],
            Tags=[permissions._boundary_policy_owner_tag(OWNER)],
        )
        self.iam.create_policy_version.assert_not_called()

    def test_exclusively_owned_boundary_can_be_updated(self):
        self.tags(permissions._boundary_policy_tags(OWNER))
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
        }
        permissions._ensure_permissions_boundary_policy(
            self.iam, self.boundary, OWNER, strict_ownership=True
        )
        self.iam.create_policy_version.assert_called_once()


if __name__ == "__main__":
    unittest.main()
