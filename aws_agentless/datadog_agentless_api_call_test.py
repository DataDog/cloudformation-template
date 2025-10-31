#!/usr/bin/env python3

import json
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

# Import the functions to test
from datadog_agentless_api_call import (
    call_datadog_agentless_api,
    is_agentless_scanning_enabled,
)


class TestCallDatadogAgentlessAPI(unittest.TestCase):
    """Test cases for call_datadog_agentless_api function"""

    def setUp(self):
        """Set up test fixtures"""
        self.base_event = {
            "ResourceProperties": {
                "TemplateVersion": "1.0.0",
                "APIKey": "0123456789abcdef0123456789abcdef",
                "APPKey": "0123456789abcdef0123456789abcdef12345678",
                "DatadogSite": "datadoghq.com",
                "AccountId": "123456789012",
                "Hosts": "true",
                "Containers": "false",
                "Lambdas": "true",
                "SensitiveData": "false",
            },
            "StackId": "arn:aws:cloudformation:us-east-1:358251252154:stack/DatadogAgentlessIntegration/22b23bca-de8b-451c-99e4-c69b9ad20ec7",
        }
        site = self.base_event["ResourceProperties"]["DatadogSite"]
        self.url = f"https://api.{site}/api/v2/agentless_scanning/accounts/aws"

    def create_mock_response(self, status_code, headers={}, data=b""):
        """Helper method to create a mock HTTP response"""
        response = Mock()
        response.status = status_code
        response.headers = headers
        response.read.return_value = data
        return response

    def create_mock_http_error(self, status_code, headers={}, data=b""):
        """Helper method to create a mock HTTPError"""
        response = self.create_mock_response(status_code, headers, data)
        return HTTPError(self.url, status_code, "Test Error", headers, response)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_success_200(self, mock_is_enabled, mock_urlopen):
        """Test successful POST request with 200 status code"""
        mock_is_enabled.return_value = False
        mock_response = self.create_mock_response(200)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "POST")

        self.assertEqual(result.status, 200)

        # Verify that the request was made with the POST method
        call_args = mock_urlopen.call_args[0][0]
        self.assertEqual(call_args.get_method(), "POST")

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_success_201(self, mock_is_enabled, mock_urlopen):
        """Test successful POST request with 201 status code"""
        mock_is_enabled.return_value = False
        mock_response = self.create_mock_response(201)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "POST")

        self.assertEqual(result.status, 201)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_success_204(self, mock_is_enabled, mock_urlopen):
        """Test successful POST request with 204 status code"""
        mock_is_enabled.return_value = False
        mock_response = self.create_mock_response(204)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "POST")

        self.assertEqual(result.status, 204)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_error_400(self, mock_is_enabled, mock_urlopen):
        """Test POST request with 400 error"""
        mock_is_enabled.return_value = False
        mock_error = self.create_mock_http_error(400)
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(HTTPError):
            call_datadog_agentless_api(self.base_event, "POST")

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_error_404(self, mock_is_enabled, mock_urlopen):
        """Test POST request with 404 error"""
        mock_is_enabled.return_value = False
        mock_error = self.create_mock_http_error(404)
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(HTTPError):
            call_datadog_agentless_api(self.base_event, "POST")

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_error_500(self, mock_is_enabled, mock_urlopen):
        """Test POST request with 500 error"""
        mock_is_enabled.return_value = False
        mock_error = self.create_mock_http_error(500)
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(HTTPError):
            call_datadog_agentless_api(self.base_event, "POST")

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_patch_when_enabled(self, mock_is_enabled, mock_urlopen):
        """Test POST request uses PATCH when agentless scanning is already enabled"""
        mock_is_enabled.return_value = True
        mock_response = self.create_mock_response(200)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "POST")

        self.assertEqual(result.status, 200)

        # Verify that the request was made with the PATCH method
        call_args = mock_urlopen.call_args[0][0]
        self.assertEqual(call_args.get_method(), "PATCH")

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_delete_success_200(self, mock_urlopen):
        """Test successful DELETE request with 200 status code"""
        mock_response = self.create_mock_response(200)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "DELETE")

        self.assertEqual(result.status, 200)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_delete_success_204(self, mock_urlopen):
        """Test successful DELETE request with 204 status code"""
        mock_response = self.create_mock_response(204)
        mock_urlopen.return_value = mock_response

        result = call_datadog_agentless_api(self.base_event, "DELETE")

        self.assertEqual(result.status, 204)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_delete_error_404_returns_error(self, mock_urlopen):
        """Test DELETE request with 404 error returns the error instead of raising"""
        mock_error = self.create_mock_http_error(404)
        mock_urlopen.side_effect = mock_error

        result = call_datadog_agentless_api(self.base_event, "DELETE")

        self.assertEqual(result.status, 404)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_delete_error_500_raises_exception(self, mock_urlopen):
        """Test DELETE request with 500 error raises exception"""
        mock_error = self.create_mock_http_error(500)
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(HTTPError):
            call_datadog_agentless_api(self.base_event, "DELETE")

    def test_unsupported_method_returns_none(self):
        """Test that unsupported HTTP methods return None"""
        result = call_datadog_agentless_api(self.base_event, "PUT")
        self.assertIsNone(result)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    @patch("datadog_agentless_api_call.is_agentless_scanning_enabled")
    def test_post_request_payload_structure(self, mock_is_enabled, mock_urlopen):
        """Test that POST request payload has correct structure"""
        mock_is_enabled.return_value = False
        mock_response = self.create_mock_response(200)
        mock_urlopen.return_value = mock_response

        call_datadog_agentless_api(self.base_event, "POST")

        # Get the request that was made
        call_args = mock_urlopen.call_args[0][0]
        request_data = call_args.data.decode("utf-8")
        payload = json.loads(request_data)

        # Verify payload structure
        self.assertIn("data", payload)
        self.assertIn("type", payload["data"])
        self.assertEqual(payload["data"]["type"], "aws_scan_options")


class TestIsAgentlessScanningEnabled(unittest.TestCase):
    """Test cases for is_agentless_scanning_enabled function"""

    def setUp(self):
        """Set up test fixtures"""
        self.account_id = "123456789012"
        self.url = f"https://api.datadoghq.com/api/v2/agentless_scanning/accounts/aws/{self.account_id}"
        self.headers = {
            "DD-API-KEY": "0123456789abcdef0123456789abcdef",
            "DD-APPLICATION-KEY": "0123456789abcdef0123456789abcdef12345678",
        }

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_enabled_returns_true(self, mock_urlopen):
        """Test that function returns True when agentless scanning is enabled"""
        mock_response = Mock()
        mock_response.status = 200
        mock_urlopen.return_value = mock_response

        result = is_agentless_scanning_enabled(self.url, self.headers)

        self.assertTrue(result)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_disabled_returns_false(self, mock_urlopen):
        """Test that function returns False when agentless scanning is disabled (404)"""
        mock_error = HTTPError(self.url, 404, "Not Found", {}, Mock())
        mock_urlopen.side_effect = mock_error

        result = is_agentless_scanning_enabled(self.url, self.headers)

        self.assertFalse(result)

    @patch("datadog_agentless_api_call.urllib.request.urlopen")
    def test_other_error_raises_exception(self, mock_urlopen):
        """Test that function raises exception for non-404 errors"""
        mock_error = HTTPError(self.url, 500, "Internal Server Error", {}, Mock())
        mock_urlopen.side_effect = mock_error

        with self.assertRaises(HTTPError):
            is_agentless_scanning_enabled(self.url, self.headers)


if __name__ == "__main__":
    unittest.main()

