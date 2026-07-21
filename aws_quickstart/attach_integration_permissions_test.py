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
    BOUNDARY_POLICY_NAMES,
    MAX_POLICY_DOCUMENTS,
    _ensure_permissions_boundary_policy,
    _validate_permissions_boundary_policy_documents,
    cleanup_permissions_boundaries,
    manage_permissions_boundaries,
)


class TestLambdaSourceEmbedding(unittest.TestCase):
    def test_cloudformation_template_uses_source_placeholder(self):
        template_path = Path(__file__).with_name("datadog_integration_permissions.yaml")
        template = template_path.read_text()

        self.assertIn(
            "      Code:\n        ZipFile: |\n          <ZIPFILE_PLACEHOLDER>\n",
            template,
        )
        self.assertEqual(template.count("<ZIPFILE_PLACEHOLDER>"), 1)

    def test_release_embeds_tested_source(self):
        release_path = Path(__file__).with_name("release.sh")
        release = release_path.read_text()

        self.assertIn(
            'cp datadog_agentless_api_call.py attach_integration_permissions.py "${TEMP_DIR}/"',
            release,
        )
        self.assertIn(
            "embed_python_source datadog_integration_permissions.yaml attach_integration_permissions.py",
            release,
        )

    def test_custom_resource_has_policy_attachment_schema_version(self):
        template_path = Path(__file__).with_name("datadog_integration_permissions.yaml")
        self.assertIn('      PolicyAttachmentSchemaVersion: "3"', template_path.read_text())

    def test_execution_role_scopes_boundary_lifecycle_to_known_policies(self):
        template_path = Path(__file__).with_name("datadog_integration_permissions.yaml")
        template = template_path.read_text()

        for action in (
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
            "iam:ListPolicyVersions",
            "iam:CreatePolicyVersion",
            "iam:DeletePolicyVersion",
            "iam:ListEntitiesForPolicy",
        ):
            self.assertIn(f"                  - {action}", template)
        for policy_name in BOUNDARY_POLICY_NAMES:
            self.assertIn(f"policy/{policy_name}", template)
        self.assertNotIn("policy/datadog-instrumenter-*-boundary", template)

    def test_old_role_detach_is_limited_to_datadog_policies(self):
        template_path = Path(__file__).with_name("datadog_integration_permissions.yaml")
        template = template_path.read_text()

        self.assertIn("                Action: iam:DetachRolePolicy", template)
        self.assertIn(
            "                Resource: !Sub "
            "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/*",
            template,
        )
        self.assertIn("                    iam:PolicyARN:", template)
        self.assertIn("                Action: iam:DeleteRolePolicy", template)


def make_iam_mock(cleanup_side_effects=True):
    iam = MagicMock()
    iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
    iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
    iam.exceptions.EntityAlreadyExistsException = type("EAE", (Exception,), {})
    if cleanup_side_effects:
        iam.detach_role_policy.side_effect = iam.exceptions.NoSuchEntityException
        iam.delete_policy.side_effect = iam.exceptions.NoSuchEntityException
    return iam


def permissions_boundary_documents(account_id="123456789012", partition="aws"):
    return [
        {
            "id": policy_name.removeprefix("datadog-instrumenter-").removesuffix("-boundary"),
            "policy_name": policy_name,
            "policy_arn": f"arn:{partition}:iam::{account_id}:policy/{policy_name}",
            "role_arn_patterns": [],
            "policy_document": {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["logs:PutLogEvents"],
                        "Resource": ["*"],
                    }
                ],
            },
        }
        for policy_name in BOUNDARY_POLICY_NAMES
    ]


def detached_arns(iam):
    return [request.kwargs["PolicyArn"] for request in iam.detach_role_policy.call_args_list]


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

    def test_query_parameters(self):
        url = build_instrumentation_permissions_url(
            "datadoghq.com",
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
            "123456789012",
            "aws-us-gov",
        )
        pairs = self._query_pairs(url)
        resource_types = [value for key, value in pairs if key == "resource_type"]
        self.assertEqual(
            resource_types,
            ["aws:ec2:instance", "aws:ecs:cluster", "aws:eks:cluster"],
        )
        self.assertIn(("account_id", "123456789012"), pairs)
        self.assertIn(("partition", "aws-us-gov"), pairs)
        self.assertIn(("chunked", "true"), pairs)


class TestAttachInstrumentationPermissions(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()
        self.manage_boundaries = self.enterContext(
            patch("attach_integration_permissions.manage_permissions_boundaries")
        )
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
        self.iam.list_attached_role_policies.return_value = {"AttachedPolicies": []}
        self.iam.get_account_summary.return_value = {
            "SummaryMap": {"AttachedPoliciesPerRoleQuota": 10}
        }
        self.role_name = "DatadogIntegrationRole"
        self.account_id = "123456789012"
        self.partition = "aws"
        self.site = "datadoghq.com"
        self.boundary_documents = permissions_boundary_documents(
            self.account_id,
            self.partition,
        )

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
        attributes.setdefault(
            "permissions_boundary_policy_documents",
            self.boundary_documents,
        )
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
        self.manage_boundaries.assert_called_once_with(self.iam, self.boundary_documents)
        self.assertEqual(
            [
                request.kwargs["PolicyName"]
                for request in self.iam.create_policy.call_args_list
            ],
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
    def test_fetch_failure_preserves_existing_policies(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )

        self._attach(["aws:ec2:instance"])

        self.iam.create_policy.assert_not_called()
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_missing_policy_documents_preserves_existing_policies(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            permissions=[["ec2:DescribeInstances"]]
        )

        self._attach(["aws:ec2:instance"])

        self.iam.create_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_missing_boundary_documents_preserves_existing_policies(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            policy_documents=[{"Version": "2012-10-17", "Statement": []}],
            permissions_boundary_policy_documents=None,
        )

        self._attach(["aws:ec2:instance"])

        self.manage_boundaries.assert_not_called()
        self.iam.create_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_manages_boundaries_before_replacing_instrumentation_policies(
        self,
        mock_urlopen,
        mock_cleanup,
    ):
        order = []
        self.manage_boundaries.side_effect = lambda *_: order.append("boundaries")
        mock_cleanup.side_effect = lambda *_, **__: order.append("cleanup")
        mock_urlopen.return_value = self._mock_response(
            policy_documents=[{"Version": "2012-10-17", "Statement": []}]
        )

        self._attach(["aws:ec2:instance"], fail_on_error=True)

        self.assertEqual(order, ["boundaries", "cleanup"])

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_rejects_more_than_ten_policy_documents(self, mock_urlopen):
        document = {"Version": "2012-10-17", "Statement": []}
        mock_urlopen.return_value = self._mock_response(
            policy_documents=[document] * (MAX_POLICY_DOCUMENTS + 1)
        )

        with self.assertRaisesRegex(Exception, "at most 10"):
            self._attach(["aws:ec2:instance"], fail_on_error=True)

        self.iam.create_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_per_document_failure_is_swallowed_and_others_continue(self, mock_urlopen):
        documents = [
            {"Version": "2012-10-17", "Statement": [{"Action": [f"chunk{i}:Action"]}]}
            for i in range(3)
        ]
        mock_urlopen.return_value = self._mock_response(policy_documents=documents)
        self.iam.create_policy.side_effect = [
            {"Policy": {"Arn": "arn:aws:iam::123:policy/A"}},
            Exception("EntityAlreadyExists"),
            {"Policy": {"Arn": "arn:aws:iam::123:policy/C"}},
        ]

        self._attach(["aws:ec2:instance"])

        self.assertEqual(self.iam.create_policy.call_count, 3)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_replacement_creation_failure_is_not_swallowed(self, mock_urlopen):
        documents = [
            {"Version": "2012-10-17", "Statement": [{"Action": [f"chunk{i}:Action"]}]}
            for i in range(3)
        ]
        mock_urlopen.return_value = self._mock_response(policy_documents=documents)
        self.iam.create_policy.side_effect = [
            {"Policy": {"Arn": "arn:aws:iam::123:policy/A"}},
            Exception("MalformedPolicyDocument"),
            {"Policy": {"Arn": "arn:aws:iam::123:policy/C"}},
        ]

        with self.assertRaisesRegex(Exception, "MalformedPolicyDocument"):
            self._attach(
                ["aws:ec2:instance", "aws:eks:cluster"],
                previous_resource_types=["aws:ec2:instance"],
            )

        self.assertEqual(self.iam.create_policy.call_count, 2)
        self.assertEqual(self.iam.attach_role_policy.call_count, 1)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_replacement_cleanup_failure_is_not_swallowed(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            policy_documents=[{"Version": "2012-10-17", "Statement": []}]
        )
        self.iam.detach_role_policy.side_effect = Exception("AccessDenied")

        with self.assertRaisesRegex(Exception, "AccessDenied"):
            self._attach(
                ["aws:eks:cluster"],
                previous_resource_types=["aws:ec2:instance"],
            )

        self.iam.create_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_replacement_preparation_failure_is_not_swallowed(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )

        with self.assertRaises(Exception):
            self._attach(
                ["aws:eks:cluster"],
                previous_resource_types=["aws:ec2:instance"],
            )

        self.iam.detach_role_policy.assert_not_called()
        self.iam.create_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_fetch_failure(self, mock_urlopen):
        mock_urlopen.side_effect = HTTPError(
            "u", 500, "boom", {}, BytesIO(b'{"errors":["upstream down"]}')
        )
        with self.assertRaises(Exception):
            self._attach(["aws:ec2:instance"], fail_on_error=True)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_attach_failure(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_response(
            policy_documents=[{"Version": "2012-10-17", "Statement": []}]
        )
        self.iam.create_policy.side_effect = Exception("AccessDenied")
        with self.assertRaises(Exception):
            self._attach(["aws:ec2:instance"], fail_on_error=True)

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_quota_failure_happens_before_policy_mutation(self, mock_urlopen):
        documents = [
            {"Version": "2012-10-17", "Statement": [{"Action": [f"chunk{i}:Action"]}]}
            for i in range(2)
        ]
        mock_urlopen.return_value = self._mock_response(policy_documents=documents)
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
        self.iam.attach_role_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_quota_preflight_excludes_policies_that_cleanup_replaces(self, mock_urlopen):
        documents = [
            {"Version": "2012-10-17", "Statement": [{"Action": [f"chunk{i}:Action"]}]}
            for i in range(2)
        ]
        mock_urlopen.return_value = self._mock_response(policy_documents=documents)
        existing_name = f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1"
        self.iam.list_attached_role_policies.return_value = {
            "AttachedPolicies": [
                {
                    "PolicyName": f"CustomerPolicy{index}",
                    "PolicyArn": f"arn:aws:iam::123:policy/CustomerPolicy{index}",
                }
                for index in range(8)
            ]
            + [
                {
                    "PolicyName": existing_name,
                    "PolicyArn": f"arn:aws:iam::123:policy/{existing_name}",
                }
            ]
        }

        self._attach(["aws:ec2:instance"], fail_on_error=True)

        self.assertEqual(self.iam.create_policy.call_count, 2)
        self.assertEqual(self.iam.attach_role_policy.call_count, 2)


class TestPermissionsBoundaryPolicies(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock(cleanup_side_effects=False)
        self.boundary = permissions_boundary_documents()[0]
        self.policy_arn = self.boundary["policy_arn"]

    def test_validates_exact_boundary_contract(self):
        documents = permissions_boundary_documents(partition="aws-us-gov")

        validated = _validate_permissions_boundary_policy_documents(
            documents,
            "123456789012",
            "aws-us-gov",
        )

        self.assertEqual(
            [document["policy_name"] for document in validated],
            list(BOUNDARY_POLICY_NAMES),
        )

    def test_rejects_missing_boundary(self):
        with self.assertRaisesRegex(Exception, "omitted permissions boundaries"):
            _validate_permissions_boundary_policy_documents(
                permissions_boundary_documents()[:-1],
                "123456789012",
                "aws",
            )

    def test_rejects_unexpected_boundary(self):
        documents = permissions_boundary_documents()
        documents[0]["policy_name"] = "customer-policy"

        with self.assertRaisesRegex(Exception, "unexpected permissions boundary"):
            _validate_permissions_boundary_policy_documents(
                documents,
                "123456789012",
                "aws",
            )

    def test_rejects_mismatched_boundary_arn(self):
        documents = permissions_boundary_documents()
        documents[0]["policy_arn"] = "arn:aws:iam::123456789012:policy/customer-policy"

        with self.assertRaisesRegex(Exception, "expected"):
            _validate_permissions_boundary_policy_documents(
                documents,
                "123456789012",
                "aws",
            )

    def test_creates_missing_boundary(self):
        self.iam.get_policy.side_effect = self.iam.exceptions.NoSuchEntityException()

        _ensure_permissions_boundary_policy(self.iam, self.boundary)

        request = self.iam.create_policy.call_args.kwargs
        self.assertEqual(request["PolicyName"], self.boundary["policy_name"])
        self.assertEqual(json.loads(request["PolicyDocument"]), self.boundary["policy_document"])
        self.iam.attach_role_policy.assert_not_called()
        self.iam.create_policy_version.assert_not_called()

    @patch("attach_integration_permissions._ensure_permissions_boundary_policy")
    def test_manages_every_returned_boundary(self, mock_ensure):
        documents = permissions_boundary_documents()

        manage_permissions_boundaries(self.iam, documents)

        self.assertEqual(
            mock_ensure.call_args_list,
            [call(self.iam, document) for document in documents],
        )

    def test_unchanged_boundary_does_not_create_version(self):
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": json.dumps(self.boundary["policy_document"])}
        }

        _ensure_permissions_boundary_policy(self.iam, self.boundary)

        self.iam.list_policy_versions.assert_not_called()
        self.iam.create_policy_version.assert_not_called()

    def test_changed_boundary_creates_default_version(self):
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
        }
        self.iam.list_policy_versions.return_value = {
            "Versions": [{"VersionId": "v1", "IsDefaultVersion": True, "CreateDate": 1}]
        }

        _ensure_permissions_boundary_policy(self.iam, self.boundary)

        request = self.iam.create_policy_version.call_args.kwargs
        self.assertEqual(request["PolicyArn"], self.policy_arn)
        self.assertEqual(json.loads(request["PolicyDocument"]), self.boundary["policy_document"])
        self.assertTrue(request["SetAsDefault"])

    def test_prunes_oldest_non_default_version_at_limit(self):
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v5"}}
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": {"Version": "2012-10-17", "Statement": []}}
        }
        self.iam.list_policy_versions.return_value = {
            "Versions": [
                {"VersionId": f"v{version}", "IsDefaultVersion": version == 5, "CreateDate": version}
                for version in range(1, 6)
            ]
        }

        _ensure_permissions_boundary_policy(self.iam, self.boundary)

        self.iam.delete_policy_version.assert_called_once_with(
            PolicyArn=self.policy_arn,
            VersionId="v1",
        )
        method_names = [method_call[0] for method_call in self.iam.method_calls]
        self.assertLess(
            method_names.index("delete_policy_version"),
            method_names.index("create_policy_version"),
        )

    def test_concurrent_create_uses_policy_created_by_other_invocation(self):
        self.iam.get_policy.side_effect = [
            self.iam.exceptions.NoSuchEntityException(),
            {"Policy": {"DefaultVersionId": "v1"}},
        ]
        self.iam.create_policy.side_effect = self.iam.exceptions.EntityAlreadyExistsException()
        waiter = self.iam.get_waiter.return_value
        self.iam.get_policy_version.return_value = {
            "PolicyVersion": {"Document": self.boundary["policy_document"]}
        }

        _ensure_permissions_boundary_policy(self.iam, self.boundary)

        self.iam.create_policy.assert_called_once()
        self.iam.get_waiter.assert_called_once_with("policy_exists")
        waiter.wait.assert_called_once_with(
            PolicyArn=self.policy_arn,
            WaiterConfig={"Delay": 1, "MaxAttempts": 20},
        )
        self.iam.create_policy_version.assert_not_called()

    def test_delete_removes_unused_boundary_and_non_default_versions(self):
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v2"}}
        self.iam.list_entities_for_policy.return_value = {
            "PolicyGroups": [],
            "PolicyUsers": [],
            "PolicyRoles": [],
            "IsTruncated": False,
        }
        self.iam.list_policy_versions.return_value = {
            "Versions": [
                {"VersionId": "v1", "IsDefaultVersion": False},
                {"VersionId": "v2", "IsDefaultVersion": True},
            ]
        }

        with patch(
            "attach_integration_permissions.BOUNDARY_POLICY_NAMES",
            (self.boundary["policy_name"],),
        ):
            cleanup_permissions_boundaries(self.iam, "123456789012", "aws")

        self.iam.delete_policy_version.assert_called_once_with(
            PolicyArn=self.policy_arn,
            VersionId="v1",
        )
        self.iam.delete_policy.assert_called_once_with(PolicyArn=self.policy_arn)

    def test_delete_retains_boundary_attached_to_role(self):
        self.iam.get_policy.return_value = {"Policy": {"DefaultVersionId": "v1"}}
        self.iam.list_entities_for_policy.return_value = {
            "PolicyRoles": [{"RoleName": "dd-eks-instrumenter-example"}],
            "IsTruncated": False,
        }

        with patch(
            "attach_integration_permissions.BOUNDARY_POLICY_NAMES",
            (self.boundary["policy_name"],),
        ):
            cleanup_permissions_boundaries(self.iam, "123456789012", "aws")

        self.iam.list_policy_versions.assert_not_called()
        self.iam.delete_policy.assert_not_called()


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

    def test_strict_base_cleanup_propagates_inline_policy_failure(self):
        self.iam.delete_role_policy.side_effect = Exception("AccessDenied")

        with self.assertRaisesRegex(Exception, "AccessDenied"):
            cleanup_existing_policies(
                self.iam,
                "MyRole",
                "123456789012",
                "aws",
                max_policies=2,
                fail_on_error=True,
            )


class TestCleanupLegacyBasePolicies(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()

    def test_only_targets_legacy_names_not_v2(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        for arn in detached_arns(self.iam):
            self.assertNotIn("-permissions-v2-", arn)

    def test_cleans_legacy_resource_collection_and_standard(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = detached_arns(self.iam)
        self.assertTrue(any(LEGACY_PREFIX_RESOURCE_COLLECTION + "-MyRole" in arn for arn in arns))
        self.iam.delete_role_policy.assert_called_once_with(
            RoleName="MyRole", PolicyName=LEGACY_POLICY_NAME_STANDARD
        )

    def test_does_not_touch_instrumentation(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        arns = detached_arns(self.iam)
        self.assertTrue(all("instrumentation" not in arn for arn in arns))


class TestManageBasePermissions(unittest.TestCase):
    stack_id = "arn:aws:cloudformation:us-east-1:123456789012:stack/test-stack/id"
    logical_resource_id = "DatadogAttachIntegrationPermissionsFunctionTrigger"

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
            "PolicyAttachmentSchemaVersion": "2",
        }
        props.update(overrides)
        return {
            "RequestType": "Create",
            "StackId": self.stack_id,
            "LogicalResourceId": self.logical_resource_id,
            "ResourceProperties": props,
        }

    def _update(self, current=None, previous=None):
        current_props = self._props(**(current or {}))["ResourceProperties"]
        previous_props = self._props(**(previous or {}))["ResourceProperties"]
        return {
            "RequestType": "Update",
            "StackId": self.stack_id,
            "LogicalResourceId": self.logical_resource_id,
            "PhysicalResourceId": "legacy-log-stream",
            "ResourceProperties": current_props,
            "OldResourceProperties": previous_props,
        }

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

    @patch("attach_integration_permissions.cleanup_permissions_boundaries")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_delete_manage_base_false_only_instrumentation(
        self, mock_cleanup_base, mock_cleanup_instr, mock_client, mock_cleanup_boundaries
    ):
        mock_client.return_value = self.iam
        event = self._props(ManageBasePermissions="false")
        event["RequestType"] = "Delete"
        handle_delete(event, None)
        mock_cleanup_base.assert_not_called()
        mock_cleanup_instr.assert_called_once()
        mock_cleanup_boundaries.assert_called_once()

    @patch("attach_integration_permissions.cleanup_permissions_boundaries")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    def test_delete_manage_base_true_cleans_both(
        self, mock_cleanup_base, mock_cleanup_instr, mock_client, mock_cleanup_boundaries
    ):
        mock_client.return_value = self.iam
        event = self._props(ManageBasePermissions="true")
        event["RequestType"] = "Delete"
        handle_delete(event, None)
        mock_cleanup_base.assert_called_once()
        mock_cleanup_instr.assert_called_once()
        mock_cleanup_boundaries.assert_called_once()

    @patch("attach_integration_permissions.cfnresponse")
    @patch("attach_integration_permissions.boto3.client")
    def test_delete_preserves_physical_resource_id(self, mock_client, mock_cfn):
        mock_client.return_value = self.iam
        event = self._props(ManageBasePermissions="false")
        event["RequestType"] = "Delete"
        event["PhysicalResourceId"] = "legacy-log-stream"

        handle_delete(event, None)

        self.assertEqual(
            mock_cfn.send.call_args.kwargs["physicalResourceId"],
            event["PhysicalResourceId"],
        )

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_create_threads_fail_on_instrumentation_error(self, mock_instr, mock_client):
        mock_client.return_value = self.iam
        handle_create_update(
            self._props(ManageBasePermissions="false", FailOnInstrumentationError="true"),
            None,
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
            self._props(ManageBasePermissions="false", FailOnInstrumentationError="true"),
            None,
        )
        self.assertEqual(mock_cfn.send.call_args.args[2], mock_cfn.FAILED)

    @patch("attach_integration_permissions.cfnresponse")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_create_uses_deterministic_physical_resource_id(
        self, mock_instr, mock_client, mock_cfn
    ):
        mock_client.return_value = self.iam

        handle_create_update(self._props(ManageBasePermissions="false"), None)

        self.assertEqual(
            mock_cfn.send.call_args.kwargs["physicalResourceId"],
            f"{self.stack_id}/{self.logical_resource_id}",
        )

    @patch("attach_integration_permissions.cfnresponse")
    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_unrelated_update_preserves_physical_id_and_skips_instrumentation(
        self, mock_instr, mock_client, mock_cfn
    ):
        mock_client.return_value = self.iam
        event = self._update(
            current={
                "ManageBasePermissions": "false",
                "ResourceCollectionPermissions": "false",
            },
            previous={
                "ManageBasePermissions": "false",
                "ResourceCollectionPermissions": "true",
            },
        )

        handle_create_update(event, None)

        mock_instr.assert_not_called()
        self.assertEqual(
            mock_cfn.send.call_args.kwargs["physicalResourceId"],
            event["PhysicalResourceId"],
        )

    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    @patch("attach_integration_permissions.attach_resource_collection_permissions")
    @patch("attach_integration_permissions.attach_standard_permissions")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    @patch("attach_integration_permissions.cleanup_legacy_base_policies")
    @patch("attach_integration_permissions.boto3.client")
    def test_role_change_attaches_new_before_cleaning_previous_target(
        self,
        mock_client,
        mock_legacy,
        mock_existing,
        mock_standard,
        mock_rc,
        mock_instr,
        mock_cleanup_instr,
    ):
        mock_client.return_value = self.iam
        events = []
        mock_legacy.side_effect = lambda _, role, *args, **kwargs: events.append(
            ("legacy", role, kwargs.get("fail_on_error", False))
        )
        mock_existing.side_effect = lambda _, role, *args, **kwargs: events.append(
            ("existing", role, kwargs.get("fail_on_error", False))
        )
        mock_standard.side_effect = lambda _, role: events.append(("standard", role))
        mock_rc.side_effect = lambda _, role: events.append(("resource-collection", role))
        mock_instr.side_effect = lambda _, role, *args, **kwargs: events.append(
            ("instrumentation", role)
        )
        mock_cleanup_instr.side_effect = lambda _, role, *args, **kwargs: events.append(
            ("cleanup-instrumentation", role, kwargs.get("fail_on_error", False))
        )
        event = self._update(
            current={
                "DatadogIntegrationRole": "NewRole",
                "ManageBasePermissions": "true",
                "InstrumentationResourceTypes": "aws:ec2:instance",
            },
            previous={
                "DatadogIntegrationRole": "OldRole",
                "ManageBasePermissions": "true",
                "InstrumentationResourceTypes": "aws:ec2:instance",
            },
        )

        handle_create_update(event, None)

        self.assertEqual(
            events,
            [
                ("legacy", "NewRole", False),
                ("existing", "NewRole", False),
                ("standard", "NewRole"),
                ("resource-collection", "NewRole"),
                ("instrumentation", "NewRole"),
                ("legacy", "OldRole", True),
                ("existing", "OldRole", True),
                ("cleanup-instrumentation", "OldRole", True),
            ],
        )

    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    @patch("attach_integration_permissions.cleanup_legacy_base_policies")
    @patch("attach_integration_permissions.boto3.client")
    def test_addon_role_change_does_not_clean_previous_base_policies(
        self,
        mock_client,
        mock_legacy,
        mock_existing,
        mock_instr,
        mock_cleanup_instr,
    ):
        mock_client.return_value = self.iam
        event = self._update(
            current={
                "DatadogIntegrationRole": "NewRole",
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:eks:cluster",
            },
            previous={
                "DatadogIntegrationRole": "OldRole",
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance",
            },
        )

        handle_create_update(event, None)

        mock_instr.assert_called_once()
        mock_legacy.assert_not_called()
        mock_existing.assert_not_called()
        mock_cleanup_instr.assert_called_once_with(
            self.iam,
            "OldRole",
            "123456789012",
            "aws",
            fail_on_error=True,
        )

    @patch("attach_integration_permissions.cfnresponse")
    @patch("attach_integration_permissions.cleanup_instrumentation_policies")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    @patch("attach_integration_permissions.attach_resource_collection_permissions")
    @patch("attach_integration_permissions.attach_standard_permissions")
    @patch("attach_integration_permissions.cleanup_existing_policies")
    @patch("attach_integration_permissions.cleanup_legacy_base_policies")
    @patch("attach_integration_permissions.boto3.client")
    def test_previous_target_cleanup_failure_fails_update_with_same_physical_id(
        self,
        mock_client,
        mock_legacy,
        mock_existing,
        mock_standard,
        mock_rc,
        mock_instr,
        mock_cleanup_instr,
        mock_cfn,
    ):
        mock_client.return_value = self.iam

        def cleanup_legacy(_, role, *args, **kwargs):
            if role == "OldRole":
                raise Exception("AccessDenied")

        mock_legacy.side_effect = cleanup_legacy
        event = self._update(
            current={
                "DatadogIntegrationRole": "NewRole",
                "ManageBasePermissions": "true",
                "InstrumentationResourceTypes": "aws:ec2:instance",
            },
            previous={
                "DatadogIntegrationRole": "OldRole",
                "ManageBasePermissions": "true",
                "InstrumentationResourceTypes": "aws:ec2:instance",
            },
        )

        handle_create_update(event, None)

        mock_standard.assert_called_once()
        mock_rc.assert_called_once()
        mock_instr.assert_called_once()
        mock_existing.assert_called_once()
        mock_cleanup_instr.assert_not_called()
        self.assertEqual(mock_cfn.send.call_args.args[2], mock_cfn.FAILED)
        self.assertEqual(
            mock_cfn.send.call_args.kwargs["physicalResourceId"],
            event["PhysicalResourceId"],
        )

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_schema_update_refreshes_instrumentation(self, mock_instr, mock_client):
        mock_client.return_value = self.iam
        event = self._update(
            current={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance",
                "PolicyAttachmentSchemaVersion": "3",
            },
            previous={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance",
                "PolicyAttachmentSchemaVersion": "2",
            },
        )

        handle_create_update(event, None)

        mock_instr.assert_called_once()

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_enabling_fail_on_error_refreshes_instrumentation(self, mock_instr, mock_client):
        mock_client.return_value = self.iam
        event = self._update(
            current={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance",
                "FailOnInstrumentationError": "true",
            },
            previous={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance",
                "FailOnInstrumentationError": "false",
            },
        )

        handle_create_update(event, None)

        mock_instr.assert_called_once()
        self.assertTrue(mock_instr.call_args.kwargs["fail_on_error"])

    @patch("attach_integration_permissions.boto3.client")
    @patch("attach_integration_permissions.attach_instrumentation_permissions")
    def test_resource_type_reordering_does_not_replace_policies(
        self, mock_instr, mock_client
    ):
        mock_client.return_value = self.iam
        event = self._update(
            current={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:eks:cluster,aws:ec2:instance",
            },
            previous={
                "ManageBasePermissions": "false",
                "InstrumentationResourceTypes": "aws:ec2:instance,aws:eks:cluster",
            },
        )

        handle_create_update(event, None)

        mock_instr.assert_not_called()


class TestUpgradeSafePolicyNames(unittest.TestCase):
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
