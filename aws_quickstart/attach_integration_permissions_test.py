#!/usr/bin/env python3

import json
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
    LEGACY_PREFIX_INSTRUMENTATION,
)


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
        url = build_instrumentation_permissions_url("datadoghq.eu", ["aws:ec2:instance"])
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.datadoghq.eu")
        self.assertEqual(parsed.path, "/api/unstable/instrumenter/aws/iam_permissions")

    def test_repeated_resource_type_and_chunked(self):
        url = build_instrumentation_permissions_url(
            "datadoghq.com",
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
        )
        pairs = self._query_pairs(url)
        resource_types = [v for k, v in pairs if k == "resource_type"]
        self.assertEqual(
            resource_types,
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
        )
        self.assertIn(("chunked", "true"), pairs)


class TestAttachInstrumentationPermissions(unittest.TestCase):
    def setUp(self):
        self.iam = MagicMock()
        self.iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
        self.iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
        self.iam.detach_role_policy.side_effect = self.iam.exceptions.NoSuchEntityException
        self.iam.delete_policy.side_effect = self.iam.exceptions.NoSuchEntityException
        self.role_name = "DatadogIntegrationRole"
        self.account_id = "123456789012"
        self.partition = "aws"
        self.site = "datadoghq.com"

    def _attach(self, resource_types, previous_resource_types=()):
        attach_instrumentation_permissions(
            self.iam, self.role_name, self.account_id, self.partition, self.site,
            resource_types, previous_resource_types,
        )

    def _mock_chunks_response(self, chunks):
        body = json.dumps({"data": {"attributes": {"permissions": chunks}}}).encode()
        resp = Mock()
        resp.read.return_value = body
        return resp

    def test_empty_resource_types_no_op_when_previously_empty(self):
        # Stack Create (or Update with no change) and no instrumentation requested:
        # don't touch IAM at all — there's nothing to clean up.
        self._attach([], previous_resource_types=[])
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    def test_empty_resource_types_cleans_up_when_previously_set(self):
        # Toggling instrumentation off on an Update should remove the previously-attached policies.
        self._attach([], previous_resource_types=["aws:ec2:instance"])
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.assertGreater(self.iam.detach_role_policy.call_count, 0)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_happy_path_attaches_each_chunk(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_chunks_response(
            [["ec2:Describe*"], ["ssm:SendCommand", "eks:DescribeCluster"]]
        )

        self._attach(["aws:ec2:instance", "aws:eks:cluster"])

        self.assertEqual(self.iam.create_policy.call_count, 2)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)

        names = [c.kwargs["PolicyName"] for c in self.iam.create_policy.call_args_list]
        self.assertEqual(
            names,
            [
                f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1",
                f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-2",
            ],
        )

        sent_request = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_request.headers.get("Dd-aws-api-call-source"), "cfn-quickstart")

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_cleans_legacy_instrumentation_after_successful_fetch(self, mock_urlopen):
        # Upgrade case: once the v2 permissions are fetched, the legacy un-suffixed instrumentation
        # policies are removed (so they don't linger), but only after the fetch succeeds.
        mock_urlopen.return_value = self._mock_chunks_response([["ec2:Describe*"]])
        self._attach(["aws:ec2:instance"])
        detached = [c.kwargs["PolicyArn"] for c in self.iam.detach_role_policy.call_args_list]
        self.assertTrue(any(LEGACY_PREFIX_INSTRUMENTATION + "-" + self.role_name in a for a in detached))

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fetch_failure_preserves_existing_policies(self, mock_urlopen):
        # Regression: a transient API failure on Update must not silently revoke the
        # previously-attached instrumentation policies — including the legacy un-suffixed
        # ones during an upgrade, since their cleanup is deferred until after a successful
        # fetch. The function must neither call detach_role_policy / delete_policy nor raise.
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )

        self._attach(["aws:ec2:instance"])

        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_per_chunk_failure_is_swallowed_and_others_continue(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_chunks_response(
            [["chunk1:Action"], ["chunk2:Action"], ["chunk3:Action"]]
        )
        self.iam.create_policy.side_effect = [
            {"Policy": {"Arn": "arn:aws:iam::123:policy/A"}},
            Exception("EntityAlreadyExists"),
            {"Policy": {"Arn": "arn:aws:iam::123:policy/C"}},
        ]

        self._attach(["aws:ec2:instance"])

        self.assertEqual(self.iam.create_policy.call_count, 3)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_fetch_failure(self, mock_urlopen):
        # Add-on mode (fail_on_error=True): a fetch failure must propagate so the stack fails
        # instead of silently reporting SUCCESS with nothing attached.
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )
        with self.assertRaises(Exception):
            attach_instrumentation_permissions(
                self.iam, self.role_name, self.account_id, self.partition, self.site,
                ["aws:ec2:instance"], (), fail_on_error=True,
            )

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_attach_failure(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_chunks_response([["chunk1:Action"]])
        self.iam.create_policy.side_effect = Exception("AccessDenied")
        with self.assertRaises(Exception):
            attach_instrumentation_permissions(
                self.iam, self.role_name, self.account_id, self.partition, self.site,
                ["aws:ec2:instance"], (), fail_on_error=True,
            )


class TestCleanup(unittest.TestCase):
    def setUp(self):
        self.iam = MagicMock()
        self.iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
        self.iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
        self.iam.detach_role_policy.side_effect = self.iam.exceptions.NoSuchEntityException
        self.iam.delete_policy.side_effect = self.iam.exceptions.NoSuchEntityException

    def test_cleanup_existing_does_not_touch_instrumentation(self):
        cleanup_existing_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=2)

        detached = [c.kwargs["PolicyArn"] for c in self.iam.detach_role_policy.call_args_list]
        self.assertTrue(all(BASE_POLICY_PREFIX_INSTRUMENTATION not in arn for arn in detached))
        self.assertTrue(any(BASE_POLICY_PREFIX_RESOURCE_COLLECTION in arn for arn in detached))

    def test_cleanup_instrumentation_targets_only_instrumentation_prefix(self):
        cleanup_instrumentation_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=2)

        detached = [c.kwargs["PolicyArn"] for c in self.iam.detach_role_policy.call_args_list]
        self.assertEqual(len(detached), 2)
        self.assertTrue(all(BASE_POLICY_PREFIX_INSTRUMENTATION in arn for arn in detached))


class TestCleanupLegacyBasePolicies(unittest.TestCase):
    # Removing the old un-suffixed base policies before attaching the v2 ones is what keeps both
    # generations from sitting attached at once during an in-place upgrade (IAM managed-policy limit).
    def setUp(self):
        self.iam = MagicMock()
        self.iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
        self.iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
        self.iam.detach_role_policy.side_effect = self.iam.exceptions.NoSuchEntityException
        self.iam.delete_policy.side_effect = self.iam.exceptions.NoSuchEntityException

    def _detached_arns(self):
        return [c.kwargs["PolicyArn"] for c in self.iam.detach_role_policy.call_args_list]

    def test_only_targets_legacy_names_not_v2(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        for arn in self._detached_arns():
            # Legacy managed-policy ARNs must never carry the -v2 generation segment.
            self.assertNotIn("-permissions-v2-", arn)

    def test_cleans_legacy_resource_collection_and_standard(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = self._detached_arns()
        self.assertTrue(any(LEGACY_PREFIX_RESOURCE_COLLECTION + "-MyRole" in a for a in arns))
        self.iam.delete_role_policy.assert_called_once_with(
            RoleName="MyRole", PolicyName=LEGACY_POLICY_NAME_STANDARD
        )

    def test_does_not_touch_instrumentation(self):
        # Legacy instrumentation cleanup is deferred to attach_instrumentation_permissions (post-fetch).
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = self._detached_arns()
        self.assertTrue(all(LEGACY_PREFIX_INSTRUMENTATION + "-MyRole" not in a for a in arns))


class TestManageBasePermissions(unittest.TestCase):
    # ManageBasePermissions gates the standard + resource-collection policies. The role-creation
    # path sets it true (manage everything); the post-setup add-on sets it false so it manages only
    # the instrumentation policies and never touches the standard/resource-collection policies the
    # role stack owns.
    def setUp(self):
        self.iam = MagicMock()
        self.iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
        self.iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})

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
        # Add-on mode must not touch the role stack's standard/resource-collection policies.
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
        # Add-on mode: a propagated instrumentation failure must surface as a FAILED response.
        mock_client.return_value = self.iam
        mock_instr.side_effect = Exception("AccessDenied")
        handle_create_update(
            self._props(ManageBasePermissions="false", FailOnInstrumentationError="true"), None
        )
        self.assertEqual(mock_cfn.send.call_args.args[2], mock_cfn.FAILED)


class TestUpgradeSafePolicyNames(unittest.TestCase):
    # Guards the invariant that makes in-place upgrades from the inline-trigger era safe: the names
    # this template manages must be disjoint from the names the legacy (<= v4.13) Delete handler
    # removes, so the old handler can never wipe a policy this stack just attached.
    role = "DatadogIntegrationRole"

    def test_standard_policy_name_differs_from_legacy(self):
        self.assertNotEqual(POLICY_NAME_STANDARD, LEGACY_POLICY_NAME_STANDARD)

    def test_managed_policy_names_disjoint_from_legacy(self):
        def names(prefix):
            return {f"{prefix}-{self.role}-{i+1}" for i in range(10)}

        new_names = names(BASE_POLICY_PREFIX_RESOURCE_COLLECTION) | names(BASE_POLICY_PREFIX_INSTRUMENTATION)
        legacy_names = names(LEGACY_PREFIX_RESOURCE_COLLECTION) | names(LEGACY_PREFIX_INSTRUMENTATION)
        self.assertEqual(new_names & legacy_names, set())


if __name__ == "__main__":
    unittest.main()
