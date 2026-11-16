"""Exercise the Organizations template's inline API Lambda and SaaS wiring."""

from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError

import yaml


class TemplateLoader(yaml.BaseLoader):
    """Preserve CloudFormation intrinsics and string-valued parameters."""


def intrinsic(loader, tag, node):
    value = getattr(loader, f"construct_{node.id}")(node)
    return {tag if tag in ("Ref", "Condition") else f"Fn::{tag}": value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def load_template():
    return yaml.load(
        Path(__file__).with_name("main_organizations.yaml").read_text(),
        Loader=TemplateLoader,
    )


def api_response(status=200, body=None):
    response = MagicMock()
    response.status = status
    response.getcode.return_value = status
    response.read.return_value = json.dumps(body).encode() if body is not None else b""
    response.__enter__.return_value = response
    return response


class InlineLambdaTestCase(unittest.TestCase):
    def setUp(self):
        template = load_template()
        source = template["Resources"]["DatadogAPICallFunction"]["Properties"]["Code"]["ZipFile"]
        self.cfn = Mock()
        self.namespace = {"__name__": "organizations_api_lambda"}
        with patch.dict(sys.modules, {"cfnresponse": self.cfn}), patch("signal.signal"):
            exec(compile(source, "main_organizations.yaml:DatadogAPICallFunction", "exec"), self.namespace)
        self.context = Mock()
        self.context.invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:datadog-api"
        self.urlopen = self.enterContext(patch("urllib.request.urlopen"))
        self.urlopen.return_value = api_response(201)

    def invoke(self, event):
        self.namespace["handler"](event, self.context)

    def sent_response(self):
        self.cfn.send.assert_called_once()
        response = self.cfn.send.call_args.kwargs
        return response["responseStatus"], response["responseData"], response.get("physicalResourceId")


class TestAgentlessActivation(InlineLambdaTestCase):
    def setUp(self):
        super().setUp()
        self.event = {
            "RequestType": "Create",
            "ResourceType": "Custom::DatadogAgentlessActivation",
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/StackSet-Datadog/test-id",
            "LogicalResourceId": "AgentlessActivation",
            "ResourceProperties": {
                "APIKey": "test-api-key-do-not-log",
                "APPKey": "test-app-key-do-not-log",
                "ApiURL": "datadoghq.eu",
                "AccountId": "123456789012",
                "SensitiveData": "false",
                "TemplateVersion": "test-version",
                "SaaSScanningPolicyArn": "arn:aws:iam::123456789012:policy/agentless-saas",
            },
        }
        self.url = "https://api.datadoghq.eu/api/v2/agentless_scanning/accounts/aws"

    def http_error(self, status):
        error = HTTPError(
            self.url,
            status,
            "private upstream details test-api-key-do-not-log test-app-key-do-not-log",
            {},
            BytesIO(b'{"errors": ["private response body"]}'),
        )
        self.addCleanup(error.close)
        return error

    def test_create_sends_all_options_and_installation_metadata(self):
        for sensitive_data in ("false", "true"):
            with self.subTest(sensitive_data=sensitive_data):
                self.cfn.send.reset_mock()
                self.urlopen.reset_mock()
                self.event["ResourceProperties"]["SensitiveData"] = sensitive_data
                self.invoke(self.event)

                self.urlopen.assert_called_once()
                self.assertEqual(self.urlopen.call_args.kwargs, {"timeout": 10})
                request = self.urlopen.call_args.args[0]
                self.assertEqual(request.full_url, self.url)
                self.assertEqual(request.get_method(), "POST")
                payload = json.loads(request.data)
                self.assertEqual(payload["data"], {
                    "type": "aws_scan_options",
                    "id": "123456789012",
                    "attributes": {
                        "vuln_host_os": True,
                        "vuln_containers_os": True,
                        "lambda": True,
                        "compliance_host": True,
                        "sensitive_data": sensitive_data == "true",
                    },
                })
                meta = payload["meta"]
                self.assertEqual(meta["installation_mode"], "cloudformation")
                self.assertEqual(meta["installation_version"], "test-version")
                self.assertEqual(meta["cloudformation_stack_id"], self.event["StackId"])
                self.assertEqual(
                    meta["resources"]["saas_scanning_policy_arn"],
                    self.event["ResourceProperties"]["SaaSScanningPolicyArn"],
                )
                self.assertEqual(meta["resources"]["invoked_function_arn"], self.context.invoked_function_arn)
                headers = {name.lower(): value for name, value in request.header_items()}
                self.assertEqual(headers["dd-api-key"], self.event["ResourceProperties"]["APIKey"])
                self.assertEqual(headers["dd-application-key"], self.event["ResourceProperties"]["APPKey"])
                self.assertEqual(headers["dd-call-source"], "cfn-organizations")
                self.assertEqual(headers["dd-installation-version"], "test-version")
                self.assertEqual(headers["content-type"], "application/vnd.api+json; charset=utf-8")
                status, _, physical_id = self.sent_response()
                self.assertEqual(status, "SUCCESS")
                self.assertEqual(physical_id, f"{self.event['StackId']}/{self.event['LogicalResourceId']}")

    def test_success_does_not_require_one_exact_http_status(self):
        self.urlopen.return_value = api_response(202)
        with self.assertNoLogs(self.namespace["LOGGER"], level="WARNING"):
            self.invoke(self.event)
        self.assertEqual(self.sent_response()[0], "SUCCESS")
        self.urlopen.assert_called_once()

    def test_existing_options_are_overwritten_after_conflict(self):
        self.urlopen.side_effect = [self.http_error(409), api_response(204)]
        with self.assertNoLogs(self.namespace["LOGGER"], level="WARNING"):
            self.invoke(self.event)
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["POST", "PATCH"])
        self.assertEqual(requests[1].full_url, self.url + "/123456789012")
        self.assertEqual(json.loads(requests[1].data), json.loads(requests[0].data))
        self.assertEqual([call.kwargs for call in self.urlopen.call_args_list], [{"timeout": 10}] * 2)
        self.assertEqual(self.sent_response()[0], "SUCCESS")

    def test_activation_errors_warn_and_succeed_without_retry_or_sensitive_details(self):
        cases = [
            ("POST HTTP error", [self.http_error(403)], ["POST"]),
            (
                "POST network error",
                [URLError("private network details test-api-key-do-not-log")],
                ["POST"],
            ),
            ("PATCH missing account", [self.http_error(409), self.http_error(404)], ["POST", "PATCH"]),
            (
                "PATCH timeout",
                [self.http_error(409), TimeoutError("private details test-app-key-do-not-log")],
                ["POST", "PATCH"],
            ),
        ]
        for name, failures, methods in cases:
            with self.subTest(name=name):
                self.cfn.send.reset_mock()
                self.urlopen.reset_mock()
                self.urlopen.side_effect = failures
                with self.assertLogs(self.namespace["LOGGER"], level="WARNING") as logs:
                    self.invoke(self.event)
                self.assertEqual(
                    [call.args[0].get_method() for call in self.urlopen.call_args_list],
                    methods,
                )
                self.assertEqual(self.sent_response()[0], "SUCCESS")
                output = "\n".join(logs.output) + repr(self.cfn.send.call_args.kwargs)
                for private in ("private", "test-api-key-do-not-log", "test-app-key-do-not-log"):
                    self.assertNotIn(private, output)

    def test_update_and_delete_do_not_modify_options(self):
        for request_type in ("Update", "Delete"):
            with self.subTest(request_type=request_type):
                self.cfn.send.reset_mock()
                self.urlopen.reset_mock()
                self.invoke({**self.event, "RequestType": request_type, "PhysicalResourceId": "existing-activation-id"})
                self.urlopen.assert_not_called()
                status, _, physical_id = self.sent_response()
                self.assertEqual(status, "SUCCESS")
                self.assertEqual(physical_id, "existing-activation-id")


class TestExistingIntegrationLifecycle(InlineLambdaTestCase):
    def setUp(self):
        super().setUp()
        self.event = {
            "RequestType": "Create",
            "ResourceType": "Custom::DatadogAWSAccountIntegration",
            "ResourceProperties": {
                "APIKey": "api-key",
                "APPKey": "app-key",
                "ApiURL": "datadoghq.com",
                "AccountId": "123456789012",
                "RoleName": "DatadogIntegrationRole",
                "AWSPartition": "aws",
                "AccountTags": ["aws_account:123456789012"],
                "CloudSecurityPostureManagement": "false",
                "DisableMetricCollection": "false",
                "DisableResourceCollection": "false",
            },
        }
        self.url = "https://api.datadoghq.com/api/v2/integration/aws/accounts"
        self.account = {
            "id": "integration-id",
            "attributes": {"auth_config": {"external_id": "external-id"}},
        }

    def test_create_still_registers_account_and_returns_external_id(self):
        self.urlopen.return_value = api_response(200, {"data": self.account})
        self.invoke(self.event)
        self.urlopen.assert_called_once()
        request = self.urlopen.call_args.args[0]
        self.assertEqual((request.get_method(), request.full_url), ("POST", self.url))
        self.assertEqual(json.loads(request.data)["data"]["type"], "account")
        status, data, _ = self.sent_response()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(data["ExternalId"], "external-id")

    def test_update_still_looks_up_account_and_returns_external_id(self):
        self.event["RequestType"] = "Update"
        self.urlopen.side_effect = [
            api_response(200, {"data": [self.account]}),
            api_response(200, {"data": self.account}),
        ]
        self.invoke(self.event)
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "PATCH"])
        self.assertEqual(requests[1].full_url, self.url + "/integration-id")
        status, data, _ = self.sent_response()
        self.assertEqual(status, "SUCCESS")
        self.assertEqual(data["ExternalId"], "external-id")

    def test_delete_still_removes_the_integration(self):
        self.event["RequestType"] = "Delete"
        self.urlopen.side_effect = [
            api_response(200, {"data": [self.account]}),
            api_response(204),
        ]
        self.invoke(self.event)
        requests = [call.args[0] for call in self.urlopen.call_args_list]
        self.assertEqual([request.get_method() for request in requests], ["GET", "DELETE"])
        self.assertEqual(requests[1].full_url, self.url + "/integration-id")
        self.assertIsNone(requests[1].data)
        self.assertEqual(self.sent_response()[0], "SUCCESS")

    def test_registration_errors_still_fail_the_stack(self):
        error = HTTPError(self.url, 403, "Forbidden", {}, BytesIO(b'{"errors": ["Forbidden"]}'))
        self.addCleanup(error.close)
        self.urlopen.side_effect = error
        self.invoke(self.event)
        self.assertEqual(self.sent_response()[0], "FAILED")


class TestAgentlessTemplate(unittest.TestCase):
    def test_permissions_and_activation_are_opt_in_together(self):
        template = load_template()
        parameters = template["Parameters"]
        self.assertEqual(parameters["AgentlessScanning"]["Default"], "false")
        self.assertEqual(parameters["AgentlessSensitiveDataScanning"]["Default"], "false")
        resources = template["Resources"]
        policy = resources["DatadogAgentlessSaaSScanningPolicy"]
        activation = resources["DatadogAgentlessActivation"]
        self.assertEqual(policy["Type"], "AWS::IAM::ManagedPolicy")
        self.assertEqual(activation["Type"], "Custom::DatadogAgentlessActivation")
        self.assertEqual(policy["Condition"], activation["Condition"])
        self.assertEqual(
            template["Conditions"][activation["Condition"]],
            {"Fn::Equals": [{"Ref": "AgentlessScanning"}, "true"]},
        )
        self.assertEqual(policy["Properties"]["Roles"], [{"Ref": "DatadogIntegrationRole"}])
        self.assertEqual(activation["DependsOn"], "DatadogAttachIntegrationPermissionsFunctionTrigger")
        props = activation["Properties"]
        self.assertEqual(props["ServiceToken"], {"Fn::GetAtt": "DatadogAPICallFunction.Arn"})
        self.assertEqual(props["AccountId"], {"Ref": "AWS::AccountId"})
        self.assertEqual(props["SensitiveData"], {"Ref": "AgentlessSensitiveDataScanning"})
        self.assertEqual(props["SaaSScanningPolicyArn"], {"Ref": "DatadogAgentlessSaaSScanningPolicy"})


if __name__ == "__main__":
    unittest.main()
