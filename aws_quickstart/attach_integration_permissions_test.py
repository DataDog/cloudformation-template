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
    render_placeholders,
    legacy_chunk_to_statement,
    resolve_instrumentation_statement_chunks,
    filter_instrumentation_statements,
    chunk_statements,
    validate_statement_chunks,
    _count_non_instrumentation_attached_policies,
    InstrumentationPolicyLimitError,
    MAX_MANAGED_POLICY_SIZE,
    MAX_ATTACHED_MANAGED_POLICIES,
    POLICY_NAME_STANDARD,
    BASE_POLICY_PREFIX_INSTRUMENTATION,
    BASE_POLICY_PREFIX_RESOURCE_COLLECTION,
    LEGACY_POLICY_NAME_STANDARD,
    LEGACY_PREFIX_RESOURCE_COLLECTION,
)


def make_iam_mock(cleanup_side_effects=True):
    iam = MagicMock()
    iam.exceptions.NoSuchEntityException = type("NSE", (Exception,), {})
    iam.exceptions.DeleteConflictException = type("DCE", (Exception,), {})
    if cleanup_side_effects:
        iam.detach_role_policy.side_effect = iam.exceptions.NoSuchEntityException
        iam.delete_policy.side_effect = iam.exceptions.NoSuchEntityException
    set_attached_policies(iam, [])
    return iam


def set_attached_policies(iam, attached_policies, extra_pages=()):
    pages = [{"AttachedPolicies": attached_policies}, *extra_pages]
    iam.get_paginator.return_value.paginate.return_value = pages


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
        self.iam = make_iam_mock()
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
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

    def _mock_statements_response(self, statements):
        body = json.dumps({"data": {"attributes": {"policy_statements": statements}}}).encode()
        resp = Mock()
        resp.read.return_value = body
        return resp

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_default_standard_statements_means_no_filtering(self, mock_urlopen):
        # attach_instrumentation_permissions must not silently fetch/apply live standard
        # permissions for filtering unless the caller explicitly supplies standard_statements —
        # the add-on path (ManageBasePermissions=false) can't verify what's actually on the role.
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": "*"}]
        )

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.account_id, self.partition, self.site,
            ["aws:ec2:instance"], (), fail_on_error=True,
        )

        self.iam.create_policy.assert_called_once()
        policy_document = json.loads(self.iam.create_policy.call_args.kwargs["PolicyDocument"])
        self.assertEqual(policy_document["Statement"][0]["Action"], ["iam:PassRole"])

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_caller_supplied_standard_statements_are_used_for_filtering(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": "*"}]
        )
        standard_statements = [{"Action": ["iam:PassRole"], "Resource": "*"}]

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.account_id, self.partition, self.site,
            ["aws:ec2:instance"], (), standard_statements=standard_statements, fail_on_error=True,
        )

        # The only action was fully covered by standard_statements, so nothing is left to attach.
        self.iam.create_policy.assert_not_called()


class TestAttachInstrumentationPermissionsPreflight(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()
        self.iam.create_policy.return_value = {"Policy": {"Arn": "arn:aws:iam::123:policy/X"}}
        self.role_name = "DatadogIntegrationRole"
        self.account_id = "123456789012"
        self.partition = "aws"
        self.site = "datadoghq.com"

    def _mock_statements_response(self, statements):
        body = json.dumps({"data": {"attributes": {"policy_statements": statements}}}).encode()
        resp = Mock()
        resp.read.return_value = body
        return resp

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_oversize_candidate_set_preserves_existing_policies(self, mock_urlopen):
        # A single statement whose Action list alone exceeds the managed-policy size limit can
        # never be validly chunked; the preflight check must fail before any existing
        # instrumentation policy is detached, so the previous (working) set stays attached.
        huge_action_list = [f"service{i}:Action{i}" for i in range(2000)]
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": huge_action_list, "Resource": "*"}]
        )

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.account_id, self.partition, self.site,
            ["aws:ec2:instance"], (),
        )

        self.iam.create_policy.assert_not_called()
        self.iam.detach_role_policy.assert_not_called()
        self.iam.delete_policy.assert_not_called()

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_fail_on_error_raises_on_preflight_failure(self, mock_urlopen):
        huge_action_list = [f"service{i}:Action{i}" for i in range(2000)]
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": huge_action_list, "Resource": "*"}]
        )

        with self.assertRaises(InstrumentationPolicyLimitError):
            attach_instrumentation_permissions(
                self.iam, self.role_name, self.account_id, self.partition, self.site,
                ["aws:ec2:instance"], (), fail_on_error=True,
            )

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_existing_non_instrumentation_policies_count_against_the_quota(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        )
        set_attached_policies(
            self.iam, [{"PolicyName": f"SomeOtherPolicy{i}"} for i in range(MAX_ATTACHED_MANAGED_POLICIES)]
        )

        with self.assertRaises(InstrumentationPolicyLimitError):
            attach_instrumentation_permissions(
                self.iam, self.role_name, self.account_id, self.partition, self.site,
                ["aws:ec2:instance"], (), fail_on_error=True,
            )

    @patch("attach_integration_permissions.urllib.request.urlopen")
    def test_existing_instrumentation_policies_do_not_count_against_the_quota(self, mock_urlopen):
        # The instrumentation policies about to be replaced by cleanup_instrumentation_policies
        # must not count against the quota for the replacement set.
        mock_urlopen.return_value = self._mock_statements_response(
            [{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        )
        set_attached_policies(
            self.iam,
            [
                {"PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-{i+1}"}
                for i in range(MAX_ATTACHED_MANAGED_POLICIES)
            ],
        )

        attach_instrumentation_permissions(
            self.iam, self.role_name, self.account_id, self.partition, self.site,
            ["aws:ec2:instance"], (), fail_on_error=True,
        )

        self.iam.create_policy.assert_called_once()


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
    # Removing the old un-suffixed base policies before attaching the v2 ones is what keeps both
    # generations from sitting attached at once during an in-place upgrade (IAM managed-policy limit).
    def setUp(self):
        self.iam = make_iam_mock()

    def test_only_targets_legacy_names_not_v2(self):
        cleanup_legacy_base_policies(self.iam, "MyRole", "123456789012", "aws", max_policies=3)
        for arn in detached_arns(self.iam):
            # Legacy managed-policy ARNs must never carry the -v2 generation segment.
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
    # ManageBasePermissions gates the standard + resource-collection policies. The role-creation
    # path sets it true (manage everything); the post-setup add-on sets it false so it manages only
    # the instrumentation policies and never touches the standard/resource-collection policies the
    # role stack owns.
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
        # The role-creation path must filter against the exact permissions it just attached.
        self.assertEqual(
            mock_instr.call_args.kwargs["standard_statements"],
            [{"Action": mock_standard.return_value, "Resource": "*"}],
        )

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
        # It also can't verify the role's actual standard permissions, so it must not filter.
        self.assertEqual(mock_instr.call_args.kwargs["standard_statements"], [])

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
    # Guards the invariant that makes the inline-trigger era safe: every policy name this template
    # attaches must be disjoint from the un-suffixed names the legacy (<= v4.13) Delete handler removes,
    # so the old handler can never wipe a policy this stack attached. This covers instrumentation too —
    # the add-on attaches instrumentation policies against an existing role, and a later upgrade of that
    # role's stack removes the old trigger, whose unconditional instrumentation cleanup would otherwise
    # delete them.
    role = "DatadogIntegrationRole"
    # Un-suffixed prefix the legacy trigger deletes instrumentation policies by.
    LEGACY_PREFIX_INSTRUMENTATION = "datadog-aws-integration-instrumentation-permissions"

    def _names(self, prefix):
        return {f"{prefix}-{self.role}-{i+1}" for i in range(10)}

    def test_standard_policy_name_differs_from_legacy(self):
        self.assertNotEqual(POLICY_NAME_STANDARD, LEGACY_POLICY_NAME_STANDARD)

    def test_resource_collection_names_disjoint_from_legacy(self):
        self.assertEqual(
            self._names(BASE_POLICY_PREFIX_RESOURCE_COLLECTION) & self._names(LEGACY_PREFIX_RESOURCE_COLLECTION),
            set(),
        )

    def test_instrumentation_names_disjoint_from_legacy(self):
        self.assertEqual(
            self._names(BASE_POLICY_PREFIX_INSTRUMENTATION) & self._names(self.LEGACY_PREFIX_INSTRUMENTATION),
            set(),
        )


class TestRenderPlaceholders(unittest.TestCase):
    def test_renders_in_string(self):
        self.assertEqual(
            render_placeholders("arn:${Partition}:iam::${AccountId}:role/x", "123456789012", "aws"),
            "arn:aws:iam::123456789012:role/x",
        )

    def test_renders_recursively_in_list_and_dict(self):
        value = {
            "Resource": ["arn:${Partition}:iam::${AccountId}:role/x", "arn:${Partition}:s3:::bucket"],
            "Condition": {"StringEquals": {"iam:PassedToService": ["ec2.${Partition}.example"]}},
        }
        rendered = render_placeholders(value, "123456789012", "aws-cn")
        self.assertEqual(
            rendered["Resource"],
            ["arn:aws-cn:iam::123456789012:role/x", "arn:aws-cn:s3:::bucket"],
        )
        self.assertEqual(
            rendered["Condition"]["StringEquals"]["iam:PassedToService"],
            ["ec2.aws-cn.example"],
        )

    def test_leaves_non_placeholder_values_unchanged(self):
        self.assertEqual(render_placeholders("Allow", "123456789012", "aws"), "Allow")
        self.assertEqual(render_placeholders(42, "123456789012", "aws"), 42)


class TestResolveInstrumentationStatementChunks(unittest.TestCase):
    def test_legacy_fallback_preserves_chunk_boundaries(self):
        attributes = {"permissions": [["ec2:DescribeInstances"], ["iam:GetRole", "iam:PassRole"]]}
        chunks = resolve_instrumentation_statement_chunks(attributes, "123456789012", "aws", [])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], [legacy_chunk_to_statement(["ec2:DescribeInstances"])])
        self.assertEqual(chunks[1], [legacy_chunk_to_statement(["iam:GetRole", "iam:PassRole"])])

    def test_empty_policy_statements_list_is_not_treated_as_absent(self):
        # An explicitly present but empty policy_statements must not fall back to the legacy
        # permissions chunks — presence, not truthiness, decides which path is authoritative.
        attributes = {"permissions": [["ec2:DescribeInstances"]], "policy_statements": []}
        chunks = resolve_instrumentation_statement_chunks(attributes, "123456789012", "aws", [])
        self.assertEqual(chunks, [])

    def test_prefers_structured_statements_over_legacy(self):
        attributes = {
            "permissions": [["ec2:DescribeInstances"]],
            "policy_statements": [
                {
                    "Effect": "Allow",
                    "Action": ["iam:PassRole"],
                    "Resource": ["arn:${Partition}:iam::${AccountId}:role/datadog-ssm-*"],
                }
            ],
        }
        chunks = resolve_instrumentation_statement_chunks(attributes, "123456789012", "aws", [])
        statements = [s for chunk in chunks for s in chunk]
        self.assertEqual(len(statements), 1)
        self.assertEqual(statements[0]["Resource"], ["arn:aws:iam::123456789012:role/datadog-ssm-*"])

    def test_filters_against_standard_statements(self):
        attributes = {"policy_statements": [{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]}
        standard = [{"Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        chunks = resolve_instrumentation_statement_chunks(attributes, "123456789012", "aws", standard)
        self.assertEqual(chunks, [])

    def test_structured_statements_are_size_chunked(self):
        # Unlike the legacy permissions path (pre-chunked server-side), policy_statements is
        # always unchunked and must be split locally to respect the managed-policy size limit.
        statements = [
            {"Effect": "Allow", "Action": [f"service{i}:Action{i}"], "Resource": "*"} for i in range(50)
        ]
        attributes = {"policy_statements": statements}
        chunks = resolve_instrumentation_statement_chunks(attributes, "123456789012", "aws", [], max_size=200)
        self.assertGreater(len(chunks), 1)
        flattened = [s for chunk in chunks for s in chunk]
        self.assertEqual(len(flattened), 50)
        for chunk in chunks:
            size = len(json.dumps({"Version": "2012-10-17", "Statement": chunk}, separators=(',', ':')))
            self.assertLessEqual(size, 200)


class TestFilterInstrumentationStatements(unittest.TestCase):
    def test_removes_action_when_standard_is_equivalent_or_broader(self):
        statements = [{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        standard = [{"Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), [])

    def test_list_form_wildcard_resource_is_treated_as_global(self):
        # A standard statement can express "*" either as a bare string or as a single-element
        # list; both must be treated as covering any scoped instrumentation resource.
        statements = [
            {"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": ["arn:aws:iam::123:role/datadog-ssm-*"]}
        ]
        standard = [{"Action": ["iam:PassRole"], "Resource": ["*"]}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), [])

    def test_keeps_action_when_resource_differs(self):
        statements = [
            {"Effect": "Allow", "Action": ["iam:PassRole"], "Resource": ["arn:aws:iam::123:role/datadog-ssm-*"]}
        ]
        standard = [{"Action": ["iam:PassRole"], "Resource": ["arn:aws:iam::123:role/other-role"]}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), statements)

    def test_keeps_action_when_condition_differs(self):
        statements = [
            {
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": "*",
                "Condition": {"StringEquals": {"iam:PassedToService": ["ec2.amazonaws.com"]}},
            }
        ]
        standard = [
            {
                "Action": ["iam:PassRole"],
                "Resource": "*",
                "Condition": {"StringEquals": {"iam:PassedToService": ["ecs.amazonaws.com"]}},
            }
        ]
        self.assertEqual(filter_instrumentation_statements(statements, standard), statements)

    def test_keeps_scoped_statement_whose_action_is_not_in_standard(self):
        statements = [
            {
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": ["arn:aws:iam::123:role/datadog-ssm-*"],
                "Condition": {"StringEquals": {"iam:PassedToService": ["ec2.amazonaws.com"]}},
            }
        ]
        standard = [{"Action": ["ec2:DescribeInstances"], "Resource": "*"}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), statements)

    def test_drops_statement_when_every_action_is_covered(self):
        statements = [
            {"Effect": "Allow", "Action": ["ec2:DescribeInstances", "ec2:DescribeTags"], "Resource": "*"}
        ]
        standard = [{"Action": ["ec2:DescribeInstances", "ec2:DescribeTags"], "Resource": "*"}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), [])

    def test_absent_standard_condition_covers_conditioned_instrumentation(self):
        statements = [
            {
                "Effect": "Allow",
                "Action": ["iam:PassRole"],
                "Resource": "*",
                "Condition": {"StringEquals": {"iam:PassedToService": ["ec2.amazonaws.com"]}},
            }
        ]
        standard = [{"Action": ["iam:PassRole"], "Resource": "*"}]
        self.assertEqual(filter_instrumentation_statements(statements, standard), [])


class TestChunkAndValidateStatements(unittest.TestCase):
    def test_chunk_statements_never_exceeds_max_size(self):
        statements = [
            {"Effect": "Allow", "Action": [f"service{i}:Action{i}"], "Resource": "*"} for i in range(30)
        ]
        chunks = chunk_statements(statements, max_size=200)
        for chunk in chunks:
            size = len(json.dumps({"Version": "2012-10-17", "Statement": chunk}, separators=(',', ':')))
            self.assertLessEqual(size, 200)
        self.assertEqual(sum(len(c) for c in chunks), len(statements))

    def test_chunk_statements_empty_input_returns_no_chunks(self):
        self.assertEqual(chunk_statements([]), [])

    def test_chunk_statements_oversize_single_statement_gets_its_own_chunk(self):
        huge = {"Effect": "Allow", "Action": [f"service{i}:Action{i}" for i in range(500)], "Resource": "*"}
        chunks = chunk_statements([huge], max_size=200)
        self.assertEqual(chunks, [[huge]])

    def test_validate_statement_chunks_passes_within_limits(self):
        chunks = [[{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]]
        validate_statement_chunks(chunks, ["aws:ec2:instance"], existing_attached_count=0)

    def test_validate_statement_chunks_raises_on_oversize_chunk(self):
        huge = [{"Effect": "Allow", "Action": [f"service{i}:Action{i}" for i in range(500)], "Resource": "*"}]
        with self.assertRaises(InstrumentationPolicyLimitError):
            validate_statement_chunks([huge], ["aws:ec2:instance"], existing_attached_count=0, max_size=200)

    def test_validate_statement_chunks_raises_when_over_policy_attachment_limit(self):
        chunks = [[{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}] for _ in range(5)]
        with self.assertRaises(InstrumentationPolicyLimitError):
            validate_statement_chunks(chunks, ["aws:ec2:instance"], existing_attached_count=8, max_policies=10)

    def test_validate_statement_chunks_accounts_for_existing_attached_count(self):
        chunks = [[{"Effect": "Allow", "Action": ["ec2:DescribeInstances"], "Resource": "*"}]]
        validate_statement_chunks(chunks, ["aws:ec2:instance"], existing_attached_count=9, max_policies=10)


class TestCountNonInstrumentationAttachedPolicies(unittest.TestCase):
    def setUp(self):
        self.iam = make_iam_mock()
        self.role_name = "DatadogIntegrationRole"

    def test_counts_only_non_instrumentation_policies(self):
        set_attached_policies(
            self.iam,
            [
                {"PolicyName": POLICY_NAME_STANDARD},
                {"PolicyName": f"{BASE_POLICY_PREFIX_RESOURCE_COLLECTION}-{self.role_name}-1"},
                {"PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1"},
                {"PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-2"},
            ],
        )
        self.assertEqual(_count_non_instrumentation_attached_policies(self.iam, self.role_name), 2)

    def test_paginates_through_truncated_results(self):
        set_attached_policies(
            self.iam,
            [{"PolicyName": "SomePolicy1"}],
            extra_pages=[
                {
                    "AttachedPolicies": [
                        {"PolicyName": "SomePolicy2"},
                        {"PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-{self.role_name}-1"},
                    ],
                },
            ],
        )
        self.assertEqual(_count_non_instrumentation_attached_policies(self.iam, self.role_name), 2)
        self.iam.get_paginator.assert_called_once_with("list_attached_role_policies")
        self.iam.get_paginator.return_value.paginate.assert_called_once_with(RoleName=self.role_name)

    def test_unrelated_policy_containing_instrumentation_prefix_still_counts(self):
        # Exact-name match, not substring — a stale policy that merely contains the
        # instrumentation prefix isn't touched by cleanup, so it must still count.
        set_attached_policies(self.iam, [{"PolicyName": f"{BASE_POLICY_PREFIX_INSTRUMENTATION}-extra-unrelated"}])
        self.assertEqual(_count_non_instrumentation_attached_policies(self.iam, self.role_name), 1)


if __name__ == "__main__":
    unittest.main()
