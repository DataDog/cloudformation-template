from pathlib import Path
import sys
import textwrap
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from io import BytesIO


TEMPLATES = ("main_workflow.yaml", "main_extended_workflow.yaml")


def workflow_source(template):
    text = (Path(__file__).parent / template).read_text()
    block = text.split("  WorkflowStatusFunction:\n", 1)[1].split("        ZipFile: |\n", 1)[1]
    lines = []
    for line in block.splitlines(keepends=True):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("".join(lines))


class TestWorkflowStatusLogging(unittest.TestCase):
    def test_inline_sources_remain_in_sync(self):
        self.assertEqual(workflow_source(TEMPLATES[0]), workflow_source(TEMPLATES[1]))

    def test_create_delete_and_skipped_events_do_not_log_properties(self):
        marker = "forged\r\nlog entry fake-credential"
        for template in TEMPLATES:
            with self.subTest(template=template):
                cfn_response = MagicMock()
                with patch.dict(sys.modules, {"cfnresponse": cfn_response}):
                    namespace = {}
                    exec(compile(workflow_source(template), template, "exec"), namespace)
                response = MagicMock()
                response.getcode.return_value = 200
                namespace["urlopen"] = MagicMock(return_value=response)
                props = {
                    "WorkflowId": marker,
                    "StepId": marker,
                    "Status": marker,
                    "Message": marker,
                    "ApiKey": marker,
                    "AppKey": marker,
                    "ApiURL": "datadoghq.com",
                }
                for request_type in ("Create", "Delete", marker):
                    event = {
                        "RequestType": request_type,
                        "ResourceProperties": props,
                        "StackId": "stack-id",
                    }
                    cfn_response.send.reset_mock()
                    with self.assertLogs(level="INFO") as logs:
                        namespace["handler"](event, None)
                    cfn_response.send.assert_called_once()
                    self.assertEqual(cfn_response.send.call_args.args[2], cfn_response.SUCCESS)
                    self.assertNotIn("fake-credential", "\n".join(logs.output))

    def test_create_exception_message_is_not_logged_or_returned(self):
        marker = "forged\r\nlog entry fake-credential"
        for template in TEMPLATES:
            with self.subTest(template=template):
                cfn_response = MagicMock()
                with patch.dict(sys.modules, {"cfnresponse": cfn_response}):
                    namespace = {}
                    exec(compile(workflow_source(template), template, "exec"), namespace)
                namespace["send_workflow_status"] = MagicMock(side_effect=RuntimeError(marker))
                event = {
                    "RequestType": "Create",
                    "ResourceProperties": {
                        "WorkflowId": marker,
                        "StepId": marker,
                        "Status": "started",
                        "Message": marker,
                        "ApiKey": marker,
                        "AppKey": marker,
                        "ApiURL": "datadoghq.com",
                    },
                }

                with self.assertLogs(level="ERROR") as logs:
                    namespace["handler"](event, None)

                cfn_response.send.assert_called_once()
                self.assertEqual(cfn_response.send.call_args.args[2], cfn_response.SUCCESS)
                self.assertNotIn("fake-credential", str(cfn_response.send.call_args.args[3]))
                self.assertNotIn("fake-credential", "\n".join(logs.output))

    def test_unexpected_success_status_is_not_logged(self):
        marker = "forged\r\nlog entry fake-credential"
        for template in TEMPLATES:
            with self.subTest(template=template):
                with patch.dict(sys.modules, {"cfnresponse": MagicMock()}):
                    namespace = {}
                    exec(compile(workflow_source(template), template, "exec"), namespace)
                response = MagicMock()
                response.getcode.return_value = marker
                response.read.return_value = b"{}"
                namespace["urlopen"] = MagicMock(return_value=response)

                with self.assertLogs(level="INFO") as logs:
                    result = namespace["send_workflow_status"](
                        marker, marker, "started", marker, marker, marker, "datadoghq.com"
                    )

                self.assertTrue(result)
                self.assertIn("Workflow status reported successfully", "\n".join(logs.output))
                self.assertNotIn("fake-credential", "\n".join(logs.output))

    def test_http_error_body_is_not_logged(self):
        marker = "forged\r\nlog entry fake-credential"
        for template in TEMPLATES:
            with self.subTest(template=template):
                with patch.dict(sys.modules, {"cfnresponse": MagicMock()}):
                    namespace = {}
                    exec(compile(workflow_source(template), template, "exec"), namespace)
                for status_code, expected in ((400, "HTTP 400"), (marker, "HTTP error")):
                    with self.subTest(status_code=repr(status_code)):
                        namespace["urlopen"] = MagicMock(
                            side_effect=HTTPError(
                                "https://example.invalid",
                                status_code,
                                marker,
                                {},
                                BytesIO(marker.encode()),
                            )
                        )
                        with self.assertLogs(level="ERROR") as logs:
                            result = namespace["send_workflow_status"](
                                marker, marker, marker, marker, marker, marker, "datadoghq.com"
                            )
                        self.assertFalse(result)
                        self.assertIn(expected, "\n".join(logs.output))
                        self.assertNotIn("fake-credential", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
