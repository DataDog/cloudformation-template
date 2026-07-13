#!/usr/bin/env python3

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch, Mock, MagicMock, call
from urllib.error import HTTPError
from urllib.parse import urlparse, parse_qsl
from io import BytesIO

if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()
if "cfnresponse" not in sys.modules:
    sys.modules["cfnresponse"] = MagicMock()

from attach_integration_permissions import (
    parse_resource_types,
    build_instrumentation_permissions_url,
    attach_instrumentation_permissions,
    cleanup_existing_policies,
    cleanup_instrumentation_policies,
    cleanup_legacy_base_policies,
    handle_create_update,
    handle_delete,
    POLICY_NAME_STANDARD,
    BASE_POLICY_PREFIX_INSTRUMENTATION,
    BASE_POLICY_PREFIX_RESOURCE_COLLECTION,
    LEGACY_POLICY_NAME_STANDARD,
    LEGACY_PREFIX_RESOURCE_COLLECTION,
)


class TestEmbeddedLambdaSource(unittest.TestCase):
    def test_cloudformation_inline_lambda_matches_tested_source(self):
        source_path = Path(__file__).with_name("attach_integration_permissions.py")
        template_path = Path(__file__).with_name("datadog_integration_permissions.yaml")
        source = source_path.read_text().rstrip("\n")
        template = template_path.read_text()
        start_marker = "      Code:\n        ZipFile: |\n"
        end_marker = "  DatadogAttachIntegrationPermissionsFunctionTrigger:\n"
        start = template.index(start_marker) + len(start_marker)
        end = template.index(end_marker, start)
        embedded = "\n".join(
            line[10:] if line else ""
            for line in template[start:end].rstrip("\n").splitlines()
        )
        self.assertEqual(embedded, source)


def make_iam_mock(cleanup_side_effects=True):
    iam = MagicMock()
    iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
    iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
    if cleanup_side_effects:
        iam.detach_role_policy.side_effect = iam.exceptions.NoSuchEntityException
        iam.delete_policy.side_effect = iam.exceptions.NoSuchEntityException
    return iam


def detached_arns(iam):
    return [c.kwargs["PolicyArn"] for c in iam.detach_role_policy.call_args_list]


class TestParseResourceTypes(unittest.TestCase):
    def test_none(self):
        self.assertEqual(parse_resource_types(None), [])

    def test_empty_string(self):
        self.assertEqual(parse_resource_types(""), [])

    def test_single(self):
        self.assertEqual(parse_resource_types("aws:ec2:instance"), ["aws:ec2:instance"])

    def test_multiple_with_whitespace(self):
        self.assertEqual(
            parse_resource_types("aws:ec2:instance, aws:ecs:cluster ,aws:eks:cluster"),
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
        )

    def test_list_input(self):
        # CFN may forward a CommaDelimitedList as a JSON array
        self.assertEqual(
            parse_resource_types(["aws:ec2:instance", " aws:ecs:cluster "]),
            ["aws:ec2:instance", "aws:ecs:cluster"],
        )

    def test_drops_empties(self):
        self.assertEqual(parse_resource_types(",,aws:ec2:instance,,"), ["aws:ec2:instance"])


class TestBuildInstrumentationURL(unittest.TestCase):
    def _query_pairs(self, url):
        return parse_qsl(urlparse(url).query)

    def test_path_and_host(self):
        url = build_instrumentation_permissions_url(
            "datadoghq.eu",
            ["aws:ec2:instance"],
            "123456789012",
            "aws",
        )
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.datadoghq.eu")
        self.assertEqual(parsed.path, "/api/unstable/instrumenter/aws/iam_permissions")

    def test_repeated_resource_type_and_chunked(self):
        url = build_instrumentation_permissions_url(
            "datadoghq.com",
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
            "123456789012",
            "aws-us-gov",
        )
        pairs = self._query_pairs(url)
        resource_types = [v for k, v in pairs if k == "resource_type"]
        self.assertEqual(
            resource_types,
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
        )
        self.assertIn(("account_id", "123456789012"), pairs)
        self.assertIn(("partition", "aws-us-gov"), pairs)
        self.assertIn(("chunked", "true"), pairs)


class TestAttachInstrumentationPermissions(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock(cleanup_side_effects=False)
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
        self.iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        self.iam.get_account_summary.return_value = {
            "SummaryMap": {
                "AttachedPoliciesPerRoleQuota": 10,
                "VersionsPerPolicyQuota": 5,
            }
        }
        self.role_name = "DatadogIntegrationRole"
        self.account_id = "123456789012"
        self.partition = "aws"
        self.site = "datadoghq.com"

    def _attach(self, resource_types, previous_resource_types=(), fail_on_error=False):
        attach_instrumentation_permissions(
            self.iam,
            self.role_name,
            self.account_id,
            self.partition,
            self.site,
            resource_types,
            previous_resource_types,
            fail_on_error=fail_on_error,
        )

    def _mock_response(self, **attributes):
        body = json.dumps({"data": {"attributes": attributes}}).encode()
        response = Mock()
        response.read.return_value = body
        return response

    def _created_policy_documents(self):
        return [
            json.loads(request.kwargs["PolicyDocument"])
            for request in self.iam.create_policy.call_args_list
        ]

    def test_empty_resource_types_no_op_when_previously_empty(self):
        self._attach([], previous_resource_types=[])
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    def test_empty_resource_types_cleans_up_when_previously_set(self):
        self._attach([], previous_resource_types=["aws:ec2:instance"])
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.assertGreater(self.iam.detach_role_policy.call_count, 0)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_happy_path_attaches_each_policy_document(self, mock_urlopen):
        documents = [
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["iam:PassRole"],
                        "Resource": ["arn:aws:iam::123456789012:role/datadog-*"],
                    }
                ],
            },
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["ec2:DescribeInstances"],
                        "Resource": ["*"],
                    }
                ],
            },
        ]
        mock_urlopen.return_value = self._mock_response(policy_documents=documents)

        self._attach(["aws:ec2:instance", "aws:eks:cluster"])

        self.assertEqual(self.iam.create_policy.call_count, 2)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)
        self.assertEqual(self._created_policy_documents(), documents)

        names = [
            request.kwargs["PolicyName"]
            for request in self.iam.create_policy.call_args_list
        ]
        self.assertEqual(
            names,
            [
                f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1",
                f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-2",
            ],
        )

        sent_request = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_request.headers.get("Dd-aws-api-call-source"), "cfn-quickstart")
        query = parse_qsl(urlparse(sent_request.full_url).query)
        self.assertIn(("account_id", self.account_id), query)
        self.assertIn(("partition", self.partition), query)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_legacy_response_is_wrapped_as_broad_policy_documents(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            permissions=[["ec2:Describe*"], ["ssm:SendCommand"]]
        )

        self._attach(["aws:ec2:instance"])

        self.assertEqual(
            [
                document["Statement"][0]["Action"]
                for document in self._created_policy_documents()
            ],
            [["ec2:Describe*"], ["ssm:SendCommand"]],
        )

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fetch_failure_preserves_existing_policies(self, mock_urlopen):
        # A transient update failure must not revoke previously attached policies.
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )

        self._attach(["aws:ec2:instance"])

        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_create_failure_rolls_back_without_attaching_partial_documents(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            permissions=[["chunk1:Action"], ["chunk2:Action"]]
        )
        self.iam.create_policy.side_effect = [
            {"Policy": {"Arn": "arn:aws:iam::123:policy/A"}},
            Exception("EntityAlreadyExists"),
        ]

        self._attach(["aws:ec2:instance"])

        self.assertEqual(self.iam.create_policy.call_count, 2)
        self.iam.attach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_any_call(PolicyArn="arn:aws:iam::123:policy/A")

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_fetch_failure(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )
        with self.assertRaises(Exception):
            self._attach(["aws:ec2:instance"], fail_on_error=True)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_attach_failure(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(permissions=[["chunk1:Action"]])
        self.iam.attach_role_policy.side_effect = Exception("AccessDenied")
        with self.assertRaises(Exception):
            self._attach(["aws:ec2:instance"], fail_on_error=True)
        self.iam.delete_policy.assert_any_call(PolicyArn="arn:aws:iam::123:policy/X")

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_quota_failure_happens_before_policy_mutation(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            permissions=[["chunk1:Action"], ["chunk2:Action"]]
        )
        self.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [
                {
                    "PolicyName": f"CustomerPolicy{index}",
                    "PolicyArn": f"arn:aws:iam::123:policy/CustomerPolicy{index}",
                }
                for index in range(9)
            ]
        }

        with self.assertRaisesRegex(RuntimeError, "room for only 1 instrumentation policies"):
            self._attach(["aws:ec2:instance"], fail_on_error=True)

        self.iam.create_policy.assert_not_called()
        self.iam.create_policy_version.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_existing_policy_is_staged_as_a_new_version(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(permissions=[["new:Action"]])
        policy_arn = (
            f"arn:aws:iam::{self.account_id}:policy/"
            f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1"
        )
        self.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{
                "PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1",
                "PolicyArn": policy_arn,
            }]
        }
        self.iam.list_policy_versions.return_value = {
            "Versions": [{"VersionId": "v1", "IsDefaultVersion": True}]
        }
        self.iam.create_policy_version.return_value = {
            "PolicyVersion": {"VersionId": "v2"}
        }

        self._attach(["aws:ec2:instance"])

        self.iam.create_policy.assert_not_called()
        self.iam.create_policy_version.assert_called_once()
        self.iam.set_default_policy_version.assert_called_once_with(
            PolicyArn=policy_arn,
            VersionId="v2",
        )
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_attach_failure_restores_previous_policy_version(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            permissions=[["updated:Action"], ["new:Action"]]
        )
        existing_arn = (
            f"arn:aws:iam::{self.account_id}:policy/"
            f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1"
        )
        self.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [{
                "PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1",
                "PolicyArn": existing_arn,
            }]
        }
        self.iam.list_policy_versions.return_value = {
            "Versions": [{"VersionId": "v1", "IsDefaultVersion": True}]
        }
        self.iam.create_policy_version.return_value = {
            "PolicyVersion": {"VersionId": "v2"}
        }
        self.iam.create_policy.return_value = {
            "Policy": {"Arn": "arn:aws:iam::123:policy/New"}
        }
        self.iam.attach_role_policy.side_effect = Exception("LimitExceeded")

        self._attach(["aws:ec2:instance"])

        self.assertEqual(
            self.iam.set_default_policy_version.call_args_list,
            [
                call(PolicyArn=existing_arn, VersionId="v2"),
                call(PolicyArn=existing_arn, VersionId="v1"),
            ],
        )
        self.iam.delete_policy_version.assert_called_with(
            PolicyArn=existing_arn,
            VersionId="v2",
        )
        self.iam.detach_role_policy.assert_called_once_with(
            RoleName=self.role_name,
            PolicyArn="arn:aws:iam::123:policy/New",
        )


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()

    def test_cleanup_existing_does_not_touch_instrumentation(self):
        cleanup_existing_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=2)

        detached = detached_arns(self.iam)
        self.assertTrue(all(BASE_POLICY_PREFIX_INSTRUMENTATION not in arn for arn in detached))
        self.assertTrue(any(BASE_POLICY_PREFIX_RESOURCE_COLLECTION in arn for arn in detached))

    def test_cleanup_instrumentation_targets_only_instrumentation_prefix(self):
        cleanup_instrumentation_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=2)

        detached = detached_arns(self.iam)
        self.assertEqual(len(detached), 2)
        self.assertTrue(all(BASE_POLICY_PREFIX_INSTRUMENTATION in arn for arn in detached))


class TestCleanupLegacyBasePolicies(unittest.TestCase):
    # Removing legacy base policies first prevents both generations consuming the role quota.
    def setUp(self):
        self.iam = make_iam_mock()

    def test_only_targets_legacy_names_not_v2(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        for arn in detached_arns(self.iam):
            self.assertNotIn("-permissions-v2-", arn)

    def test_cleans_legacy_resource_collection_and_standard(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = detached_arns(self.iam)
        self.assertTrue(any(LEGACY_PREFIX_RESOURCE_COLLECTION + "-MyRole" in a for a in arns))
        self.iam.delete_role_policy.assert_called_once_with(
            RoleName="MyRole", PolicyName=LEGACY_POLICY_NAME_STANDARD
        )

    def test_does_not_touch_instrumentation(self):
        # Base cleanup only handles standard/resource-collection; instrumentation is managed separately.
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = detached_arns(self.iam)
        self.assertTrue(all("instrumentation" not in a for a in arns))


class TestManageBasePermissions(unittest.TestCase):
    # The add-on disables base management so it cannot modify role-stack-owned policies.
    def setUp(self):
        self.iam = make_iam_mock(cleanup_side_effects=False)

    def _props(self, **overrides):
        props = {
            "DatadogIntegrationRole": "DatadogIntegrationRole",
            "AccountId": "123456789012",
            "Partition": "aws",
            "ResourceCollectionPermissions": "true",
            "InstrumentationResourceTypes": "",
            "DatadogSite": "datadoghq.com",
        }
        props.update(overrides)
        return {"RequestType": "Create", "ResourceProperties": props}

    @patch("attach_integration_permissions.cleanup_legacy_base_policies")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    @patch("attach_integration_permissions.attach_resource_collection_permissions")
    @patch("attach_integration_permissions.attach_standard_permissions")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_create_manage_base_true_attaches_base(
        self, mock_cleanup, mock_standard, mock_rc, mock_instr, mock_client, mock_legacy
    ):
        mock_client.return_value = self.iam
        handle_create_update(self._props(ManageBasePermissions="true"), None)
        mock_cleanup.assert_called_once()
        mock_standard.assert_called_once()
        mock_rc.assert_called_once()
        mock_instr.assert_called_once()
        mock_legacy.assert_called_once()

    @patch("attach_integration_permissions.cleanup_legacy_base_policies")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    @patch("attach_integration_permissions.attach_resource_collection_permissions")
    @patch("attach_integration_permissions.attach_standard_permissions")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_create_manage_base_false_only_instrumentation(
        self, mock_cleanup, mock_standard, mock_rc, mock_instr, mock_client, mock_legacy
    ):
        mock_client.return_value = self.iam
        handle_create_update(self._props(ManageBasePermissions="false"), None)
        mock_cleanup.assert_not_called()
        mock_standard.assert_not_called()
        mock_rc.assert_not_called()
        mock_instr.assert_called_once()
        mock_legacy.assert_not_called()

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_delete_manage_base_false_only_instrumentation(
        self, mock_cleanup_base, mock_cleanup_instr, mock_client
    ):
        mock_client.return_value = self.iam
        event = self._props(ManageBasePermissions="false")
        event["RequestType"] = "Delete"
        handle_delete(event, None)
        mock_cleanup_base.assert_not_called()
        mock_cleanup_instr.assert_called_once()

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_delete_manage_base_true_cleans_both(
        self, mock_cleanup_base, mock_cleanup_instr, mock_client
    ):
        mock_client.return_value = self.iam
        event = self._props(ManageBasePermissions="true")
        event["RequestType"] = "Delete"
        handle_delete(event, None)
        mock_cleanup_base.assert_called_once()
        mock_cleanup_instr.assert_called_once()

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_create_threads_fail_on_instrumentation_error(self, mock_instr, mock_client):
        mock_client.return_value = self.iam
        handle_create_update(
            self._props(ManageBasePermissions="false", FailOnInstrumentationError="true"), None
        )
        self.assertTrue(mock_instr.call_args.kwargs["fail_on_error"])

    @patch("attach_integration_permissions.cfnresponse")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_create_reports_failed_when_instrumentation_raises(
        self, mock_instr, mock_client, mock_cfn
    ):
        mock_client.return_value = self.iam
        mock_instr.side_effect = Exception("AccessDenied")
        handle_create_update(
            self._props(ManageBasePermissions="false", FailOnInstrumentationError="true"), None
        )
        self.assertEqual(mock_cfn.send.call_args.args[2], mock_cfn.FAILED)


class TestUpgradeSafePolicyNames(unittest.TestCase):
    # V2 names must be disjoint from names the <= v4.13 Delete handler removes.
    role = "DatadogIntegrationRole"
    LEGACY_PREFIX_INSTRUMENTATION = "datadog-aws-integration-instrumentation-permissions"

    def _names(self, prefix):
        return {f"{prefix}-{self.role}-{i+1}" for i in range(10)}

    def test_standard_policy_name_differs_from_legacy(self):
        self.assertNotEqual(POLICY_NAME_STANDARD, LEGACY_POLICY_NAME_STANDARD)

    def test_resource_collection_names_disjoint_from_legacy(self):
        self.assertTrue(
            self._names(BASE_POLICY_PREFIX_RESOURCE_COLLECTION).isdisjoint(
                self._names(LEGACY_PREFIX_RESOURCE_COLLECTION)
            )
        )

    def test_instrumentation_names_disjoint_from_legacy(self):
        self.assertTrue(
            self._names(BASE_POLICY_PREFIX_INSTRUMENTATION).isdisjoint(
                self._names(self.LEGACY_PREFIX_INSTRUMENTATION)
            )
        )


if __name__ == "__main__":
    unittest.main()
