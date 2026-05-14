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
    BASE_POLICY_PREFIX_INSTRUMENTATION,
    BASE_POLICY_PREFIX_RESOURCE_COLLECTION,
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
    def test_fetch_failure_preserves_existing_policies(self, mock_urlopen):
        # Regression: a transient API failure on Update must not silently revoke the
        # previously-attached instrumentation policies. The function must neither
        # call detach_role_policy / delete_policy nor raise.
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


if __name__ == "__main__":
    unittest.main()
