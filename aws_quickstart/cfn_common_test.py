#!/usr/bin/env python3

from pathlib import Path
import unittest
from unittest.mock import MagicMock

from cfn_common import physical_resource_id, send_cfn_response


def event(**overrides):
    value = {
        "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/test/id",
        "LogicalResourceId": "CustomResource",
    }
    value.update(overrides)
    return value


class TestPhysicalResourceId(unittest.TestCase):
    def test_preserves_existing_id(self):
        self.assertEqual(
            physical_resource_id(event(PhysicalResourceId="existing-id")),
            "existing-id",
        )

    def test_builds_deterministic_id(self):
        value = event()
        self.assertEqual(
            physical_resource_id(value),
            f"{value['StackId']}/{value['LogicalResourceId']}",
        )


class TestSendCfnResponse(unittest.TestCase):
    def test_sends_response_with_physical_resource_id(self):
        cfn_response = MagicMock()
        value = event()

        send_cfn_response(
            cfn_response,
            value,
            "context",
            "SUCCESS",
            {"Result": "ok"},
        )

        cfn_response.send.assert_called_once_with(
            value,
            "context",
            "SUCCESS",
            responseData={"Result": "ok"},
            physicalResourceId=f"{value['StackId']}/{value['LogicalResourceId']}",
        )


class TestInlineComposition(unittest.TestCase):
    def test_shared_helper_composes_with_each_handler(self):
        directory = Path(__file__).parent
        common = (directory / "cfn_common.py").read_text()

        for filename in (
            "attach_integration_permissions.py",
            "accept_operator_subscription.py",
        ):
            handler = (directory / filename).read_text().replace(
                "from cfn_common import send_cfn_response\n", ""
            )
            source = f"{common}\n{handler}"

            with self.subTest(filename=filename):
                self.assertNotIn("from cfn_common import", source)
                compile(source, filename, "exec")


if __name__ == "__main__":
    unittest.main()
