"""Check AgentlessScanning references in the AWS account onboarding workflow.

main_workflow.yaml passes AgentlessScanning through datadog_integration_role.yaml
to the permissions Lambda custom resource in datadog_integration_permissions.yaml.
Both completion callbacks in main_workflow.yaml reference the same parameter in
their agentless_scanning metadata, with and without the log forwarder.

These tests inspect YAML references.
"""

from pathlib import Path
import unittest

import yaml


class TemplateLoader(yaml.BaseLoader):
    """Keep CloudFormation parameter values as strings and preserve intrinsics."""


def intrinsic(loader, tag, node):
    """Represent tags as dictionaries, e.g. !Ref Name becomes {"Ref": "Name"}.

    The tagged value can be a scalar, sequence, or mapping. Other intrinsic
    functions use their long-form keys, such as Fn::GetAtt.
    """
    value = getattr(loader, f"construct_{node.id}")(node)
    return {tag if tag in ("Ref", "Condition") else f"Fn::{tag}": value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def template(name):
    """Load a template beside this test file, preserving its parameter references."""
    return yaml.load((Path(__file__).parent / name).read_text(), Loader=TemplateLoader)


class TestAgentlessWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = template("main_workflow.yaml")
        cls.role = template("datadog_integration_role.yaml")

    def test_agentless_setting_reaches_permissions_lambda(self):
        """Follow the setting from the role stack through the permissions substack
        to the custom resource that invokes the permissions Lambda.
        """
        permissions = template("datadog_integration_permissions.yaml")
        permissions_stack = self.role["Resources"]["DatadogIntegrationPermissionsStack"]
        parameters = permissions_stack["Properties"]["Parameters"]
        self.assertEqual(parameters["AgentlessScanning"], {"Ref": "AgentlessScanning"})
        trigger = permissions["Resources"]["DatadogAttachIntegrationPermissionsFunctionTrigger"]["Properties"]
        self.assertEqual(trigger["AgentlessScanning"], {"Ref": "AgentlessScanning"})

    def test_role_stack_and_completion_callbacks_receive_agentless_setting(self):
        """Use the stack parameter for both permissions and completion metadata.

        Check callbacks with and without the log forwarder so either path reports
        the customer's final selection, including an opt-out in CloudFormation.
        """
        resources = self.workflow["Resources"]
        role_parameters = resources["DatadogIntegrationRoleStack"]["Properties"]["Parameters"]
        self.assertEqual(role_parameters["AgentlessScanning"], {"Ref": "AgentlessScanning"})
        for name in ("NotifyStackCompleteWithForwarder", "NotifyStackCompleteWithoutForwarder"):
            with self.subTest(callback=name):
                metadata = resources[name]["Properties"]["Metadata"]
                self.assertEqual(metadata["agentless_scanning"], {"Ref": "AgentlessScanning"})


if __name__ == "__main__":
    unittest.main()
