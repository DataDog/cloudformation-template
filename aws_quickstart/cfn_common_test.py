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


class TestForwardingConditions(unittest.TestCase):
    TEMPLATES = (
        "main_agent_installation.yaml",
        "main_workflow.yaml",
        "main_extended_workflow.yaml",
    )

    def test_parent_templates_gate_forwarding_on_supported_resource_types_and_regions(self):
        directory = Path(__file__).parent
        condition = """  IncludeEC2:
    Fn::Not:
      - Fn::Equals:
          - !Join
            - ""
            - !Split
              - ",aws:ec2:instance,"
              - !Sub
                - ",${NormalizedResourceTypes},"
                - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
          - !Sub
            - ",${NormalizedResourceTypes},"
            - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
  IncludeEKS:
    Fn::Not:
      - Fn::Equals:
          - !Join
            - ""
            - !Split
              - ",aws:eks:cluster,"
              - !Sub
                - ",${NormalizedResourceTypes},"
                - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
          - !Sub
            - ",${NormalizedResourceTypes},"
            - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
  IncludeLambda:
    Fn::Not:
      - Fn::Equals:
          - !Join
            - ""
            - !Split
              - ",aws:lambda:function,"
              - !Sub
                - ",${NormalizedResourceTypes},"
                - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
          - !Sub
            - ",${NormalizedResourceTypes},"
            - NormalizedResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]
  # EventBridge API destinations are not supported in all AWS regions
  # https://docs.aws.amazon.com/eventbridge/latest/userguide/feature-availability.html
  SupportsEventBridgeApiDestinations:
    Fn::Not:
      - Fn::Or:
          - !Equals [!Ref AWS::Region, ap-east-2]
          - !Equals [!Ref AWS::Region, ap-southeast-5]
          - !Equals [!Ref AWS::Region, ca-west-1]
          - !Equals [!Ref AWS::Region, il-central-1]
          - !Equals [!Ref AWS::Region, mx-central-1]
          - !Equals [!Ref AWS::Region, us-gov-east-1]
          - !Equals [!Ref AWS::Region, us-gov-west-1]
  ShouldForwardEvents:
    Fn::And:
      - Condition: SupportsEventBridgeApiDestinations
      - Fn::Or:
          - Condition: IncludeEC2
          - Condition: IncludeEKS
          - Condition: IncludeLambda
"""

        for filename in self.TEMPLATES:
            with self.subTest(filename=filename):
                template = (directory / filename).read_text()
                self.assertIn(condition, template)

    def test_region_condition_only_gates_the_forwarding_stack(self):
        directory = Path(__file__).parent
        forwarding_stack = """  DatadogAgentResourceUpdateForwardingStack:
    Type: AWS::CloudFormation::Stack
    Condition: ShouldForwardEvents
"""
        for filename in self.TEMPLATES:
            with self.subTest(filename=filename):
                template = (directory / filename).read_text()
                self.assertIn(forwarding_stack, template)
                self.assertEqual(template.count("Condition: ShouldForwardEvents"), 1)
                self.assertEqual(template.count("Condition: SupportsEventBridgeApiDestinations"), 1)
                # Instrumentation permissions remain independent of regional forwarding support.
                permissions_stack = (
                    "DatadogIntegrationPermissionsStack"
                    if filename == "main_agent_installation.yaml"
                    else "DatadogIntegrationRoleStack"
                )
                stack_lines = template.split(f"  {permissions_stack}:\n", 1)[1].splitlines()
                block = []
                for line in stack_lines:
                    if line.strip() and not line.startswith("    "):
                        break
                    block.append(line)
                self.assertNotIn("    Condition:", "\n".join(block))
                self.assertIn(
                    '        InstrumentationResourceTypes: !Join [",", !Ref InstrumentationResourceTypes]',
                    block,
                )
                if filename != "main_agent_installation.yaml":
                    self.assertIn(
                        "        ResourceCollectionPermissions: !If [ResourceCollectionPermissions, true, false]",
                        block,
                    )


if __name__ == "__main__":
    unittest.main()
