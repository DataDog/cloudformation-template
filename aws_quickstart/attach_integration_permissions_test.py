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

    def test_default_site(self):
        url = build_instrumentation_permissions_url("", ["aws:ec2:instance"])
        self.assertIn("api.datadoghq.com", url)

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
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
        self.role_name = "DatadogIntegrationRole"
        self.site = "datadoghq.com"

    def _mock_chunks_response(self, chunks):
        body = json.dumps({"data": {"attributes": {"permissions": chunks}}}).encode()
        resp = Mock()
        resp.read.return_value = body
        return resp

    def test_empty_resource_types_is_noop(self):
        attach_instrumentation_permissions(self.iam, self.role_name, self.site, [])
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_happy_path_attaches_each_chunk(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_chunks_response(
            [["ec2:Describe*"], ["ssm:SendCommand", "eks:DescribeCluster"]]
        )

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.site, ["aws:ec2:instance", "aws:eks:cluster"]
        )

        # Two chunks → two create_policy + two attach_role_policy
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

        # Sanity: Dd-Aws-Api-Call-Source header is sent
        sent_request = mock_urlopen.call_args[0][0]
        self.assertEqual(sent_request.headers.get("Dd-aws-api-call-source"), "cfn-quickstart")

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fetch_failure_is_swallowed(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )

        # Must not raise
        attach_instrumentation_permissions(
            self.iam, self.role_name, self.site, ["aws:ec2:instance"]
        )
        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_per_chunk_failure_is_swallowed_and_others_continue(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_chunks_response(
            [["chunk1:Action"], ["chunk2:Action"], ["chunk3:Action"]]
        )
        # Fail the second create_policy call; the others succeed.
        self.iam.create_policy.side_effect = [
            {"Policy": {"Arn": "arn:aws:iam::123:policy/A"}},
            Exception("EntityAlreadyExists"),
            {"Policy": {"Arn": "arn:aws:iam::123:policy/C"}},
        ]

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.site, ["aws:ec2:instance"]
        )

        # All 3 chunks attempted, but only 2 attaches succeeded.
        self.assertEqual(self.iam.create_policy.call_count, 3)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)


class TestCleanupAlsoRemovesInstrumentationPolicies(unittest.TestCase):
    def test_cleanup_iterates_both_prefixes(self):
        iam = MagicMock()
        iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
        iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
        iam.detach_role_policy.side_effect = iam.exceptions.NoSuchEntityException
        iam.delete_policy.side_effect = iam.exceptions.NoSuchEntityException

        cleanup_existing_policies(iam, "MyRole", "123456789012", "aws", max_policies=2)

        detached = [c.kwargs["PolicyArn"] for c in iam.detach_role_policy.call_args_list]
        # Should have iterated 2 times over each of the 2 prefixes.
        self.assertEqual(len(detached), 4)
        self.assertIn(
            f"arn:aws:iam::123456789012:policy/{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-MyRole-1",
            detached,
        )
        self.assertIn(
            f"arn:aws:iam::123456789012:policy/{BASE_POLICY_PREFIX_INSTRUMENTATION}-MyRole-1",
            detached,
        )


if __name__ == "__main__":
    unittest.main()
