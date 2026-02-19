#!/usr/bin/env python3

import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock, MagicMock

# cfnresponse is only available in the AWS Lambda runtime, so we mock it for testing
sys.modules["cfnresponse"] = MagicMock()

from datadog_integration_api_call import (
    call_datadog_api,
    get_datadog_account,
    handler,
    extract_uuid_from_account_response,
)


def make_event(request_type, account_id="123456789012"):
    return {
        "RequestType": request_type,
        "ResourceProperties": {
            "APIKey": "0123456789abcdef0123456789abcdef",
            "APPKey": "0123456789abcdef0123456789abcdef12345678",
            "ApiURL": "datadoghq.com",
            "AccountId": account_id,
            "RoleName": "DatadogIntegrationRole",
            "AWSPartition": "aws",
            "AccountTags": ["aws_account:123456789012"],
            "CloudSecurityPostureManagement": "false",
            "DisableMetricCollection": "false",
            "DisableResourceCollection": "false",
        },
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/guid",
        "RequestId": "test-request-id",
        "LogicalResourceId": "DatadogAWSAccountIntegration",
        "ResponseURL": "https://cloudformation-custom-resource-response-useast1.s3.amazonaws.com/test",
    }


def make_mock_response(status_code, data=None):
    """Create a mock HTTP response."""
    response = Mock()
    response.getcode.return_value = status_code
    if data is not None:
        response.read.return_value = json.dumps(data).encode("utf-8")
    else:
        response.read.return_value = b""
    return response


EXISTING_ACCOUNT_RESPONSE = {
    "data": [
        {
            "id": "existing-uuid-1234",
            "type": "account",
            "attributes": {
                "aws_account_id": "123456789012",
                "auth_config": {"external_id": "ext-id-1234"},
            },
        }
    ]
}

EMPTY_ACCOUNT_RESPONSE = {"data": []}

SUCCESS_POST_RESPONSE = {
    "data": {
        "id": "new-uuid-5678",
        "type": "account",
        "attributes": {
            "aws_account_id": "123456789012",
            "auth_config": {"external_id": "ext-id-5678"},
        },
    }
}

SUCCESS_PATCH_RESPONSE = {
    "data": {
        "id": "existing-uuid-1234",
        "type": "account",
        "attributes": {
            "aws_account_id": "123456789012",
            "auth_config": {"external_id": "ext-id-1234"},
        },
    }
}


class TestHandlerCreateNewAccount(unittest.TestCase):
    """Test Create when account does NOT already exist in Datadog."""

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_create_new_account_uses_post(self, mock_urlopen, mock_cfnresponse):
        """When account doesn't exist, Create should POST."""
        event = make_event("Create")
        context = SimpleNamespace()

        # First call: get_datadog_account returns empty list
        # Second call: call_datadog_api POST succeeds
        mock_urlopen.side_effect = [
            make_mock_response(200, EMPTY_ACCOUNT_RESPONSE),
            make_mock_response(200, SUCCESS_POST_RESPONSE),
        ]

        handler(event, context)

        # Verify POST was used (second urlopen call)
        self.assertEqual(mock_urlopen.call_count, 2)
        post_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(post_request.get_method(), "POST")
        self.assertNotIn("/existing-uuid", post_request.full_url)

        # Verify SUCCESS reported to CloudFormation
        mock_cfnresponse.send.assert_called_once()
        call_kwargs = mock_cfnresponse.send.call_args
        self.assertEqual(call_kwargs[1]["responseStatus"], "SUCCESS")


class TestHandlerCreateExistingAccount(unittest.TestCase):
    """Test Create when account ALREADY exists in Datadog (the 409 fix)."""

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_create_existing_account_uses_patch(self, mock_urlopen, mock_cfnresponse):
        """When account already exists, Create should fall back to PATCH."""
        event = make_event("Create")
        context = SimpleNamespace()

        # First call: get_datadog_account returns existing account
        # Second call: call_datadog_api PATCH succeeds
        mock_urlopen.side_effect = [
            make_mock_response(200, EXISTING_ACCOUNT_RESPONSE),
            make_mock_response(200, SUCCESS_PATCH_RESPONSE),
        ]

        handler(event, context)

        # Verify PATCH was used with the existing UUID
        self.assertEqual(mock_urlopen.call_count, 2)
        patch_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(patch_request.get_method(), "PATCH")
        self.assertIn("/existing-uuid-1234", patch_request.full_url)

        # Verify SUCCESS reported to CloudFormation
        mock_cfnresponse.send.assert_called_once()
        call_kwargs = mock_cfnresponse.send.call_args
        self.assertEqual(call_kwargs[1]["responseStatus"], "SUCCESS")

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_create_get_fails_falls_through_to_post(self, mock_urlopen, mock_cfnresponse):
        """When the GET check fails (non-200), Create should still try POST."""
        event = make_event("Create")
        context = SimpleNamespace()

        # First call: get_datadog_account returns error
        # Second call: call_datadog_api POST succeeds
        mock_urlopen.side_effect = [
            make_mock_response(500, None),
            make_mock_response(200, SUCCESS_POST_RESPONSE),
        ]

        handler(event, context)

        # Verify POST was used (fell through because GET returned non-200)
        self.assertEqual(mock_urlopen.call_count, 2)
        post_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(post_request.get_method(), "POST")

        # Verify SUCCESS reported
        mock_cfnresponse.send.assert_called_once()
        call_kwargs = mock_cfnresponse.send.call_args
        self.assertEqual(call_kwargs[1]["responseStatus"], "SUCCESS")


class TestHandlerUpdate(unittest.TestCase):
    """Test Update behavior (should be unchanged)."""

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_update_uses_patch(self, mock_urlopen, mock_cfnresponse):
        """Update should GET the account UUID then PATCH."""
        event = make_event("Update")
        context = SimpleNamespace()

        mock_urlopen.side_effect = [
            make_mock_response(200, EXISTING_ACCOUNT_RESPONSE),
            make_mock_response(200, SUCCESS_PATCH_RESPONSE),
        ]

        handler(event, context)

        self.assertEqual(mock_urlopen.call_count, 2)
        patch_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(patch_request.get_method(), "PATCH")
        self.assertIn("/existing-uuid-1234", patch_request.full_url)


class TestHandlerDelete(unittest.TestCase):
    """Test Delete behavior (should be unchanged)."""

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_delete_uses_delete(self, mock_urlopen, mock_cfnresponse):
        """Delete should GET the account UUID then DELETE."""
        event = make_event("Delete")
        context = SimpleNamespace()

        mock_urlopen.side_effect = [
            make_mock_response(200, EXISTING_ACCOUNT_RESPONSE),
            make_mock_response(204, None),
        ]

        handler(event, context)

        self.assertEqual(mock_urlopen.call_count, 2)
        delete_request = mock_urlopen.call_args_list[1][0][0]
        self.assertEqual(delete_request.get_method(), "DELETE")
        self.assertIn("/existing-uuid-1234", delete_request.full_url)

    @patch("datadog_integration_api_call.cfnresponse")
    @patch("datadog_integration_api_call.urllib.request.urlopen")
    def test_delete_nonexistent_account_succeeds(self, mock_urlopen, mock_cfnresponse):
        """Delete of an account that doesn't exist should report SUCCESS."""
        event = make_event("Delete")
        context = SimpleNamespace()

        mock_urlopen.side_effect = [
            make_mock_response(200, EMPTY_ACCOUNT_RESPONSE),
        ]

        handler(event, context)

        # Should only call GET (no DELETE needed)
        self.assertEqual(mock_urlopen.call_count, 1)

        # Should still report success so stack deletion can proceed
        mock_cfnresponse.send.assert_called_once()


class TestHandlerUnexpectedRequestType(unittest.TestCase):
    """Test unexpected request type."""

    @patch("datadog_integration_api_call.cfnresponse")
    def test_unexpected_request_type_fails(self, mock_cfnresponse):
        event = make_event("Create")
        event["RequestType"] = "Invalid"
        context = SimpleNamespace()

        handler(event, context)

        mock_cfnresponse.send.assert_called_once()
        call_args = mock_cfnresponse.send.call_args
        self.assertEqual(call_args[1]["responseStatus"], "FAILED")


class TestExtractUuidFromAccountResponse(unittest.TestCase):
    """Test the extract_uuid_from_account_response helper."""

    @patch("datadog_integration_api_call.cfnresponse")
    def test_extracts_uuid_from_single_account(self, mock_cfnresponse):
        event = make_event("Update")
        context = SimpleNamespace()
        response = make_mock_response(200, EXISTING_ACCOUNT_RESPONSE)

        uuid = extract_uuid_from_account_response(event, context, response)

        self.assertEqual(uuid, "existing-uuid-1234")

    @patch("datadog_integration_api_call.cfnresponse")
    def test_returns_none_for_empty_data_on_update(self, mock_cfnresponse):
        event = make_event("Update")
        context = SimpleNamespace()
        response = make_mock_response(200, EMPTY_ACCOUNT_RESPONSE)

        uuid = extract_uuid_from_account_response(event, context, response)

        self.assertIsNone(uuid)
        mock_cfnresponse.send.assert_called_once()

    @patch("datadog_integration_api_call.cfnresponse")
    def test_returns_none_for_multiple_accounts(self, mock_cfnresponse):
        event = make_event("Update")
        context = SimpleNamespace()
        multi_response = {"data": [{"id": "uuid-1"}, {"id": "uuid-2"}]}
        response = make_mock_response(200, multi_response)

        uuid = extract_uuid_from_account_response(event, context, response)

        self.assertIsNone(uuid)
        mock_cfnresponse.send.assert_called_once()

    @patch("datadog_integration_api_call.cfnresponse")
    def test_returns_none_for_api_error(self, mock_cfnresponse):
        event = make_event("Update")
        context = SimpleNamespace()
        response = make_mock_response(403, {"errors": ["Forbidden"]})

        uuid = extract_uuid_from_account_response(event, context, response)

        self.assertIsNone(uuid)


if __name__ == "__main__":
    unittest.main()
