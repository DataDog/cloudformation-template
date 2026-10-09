from pathlib import Path
import unittest

import yaml


class TemplateLoader(yaml.BaseLoader):
    """Keep CloudFormation parameter values as strings and preserve intrinsics."""


def intrinsic(loader, tag, node):
    value = getattr(loader, f"construct_{node.id}")(node)
    return {tag if tag in ("Ref", "Condition") else f"Fn::{tag}": value}


TemplateLoader.add_multi_constructor("!", intrinsic)


def template(name):
    return yaml.load((Path(__file__).parent / name).read_text(), Loader=TemplateLoader)


def evaluate(expression, parameters):
    if not isinstance(expression, dict):
        return expression
    key, value = next(iter(expression.items()))
    if key == "Ref":
        return parameters[value]
    operands = [evaluate(item, parameters) for item in value]
    if key == "Fn::Equals":
        return operands[0] == operands[1]
    raise AssertionError(f"Unsupported expression: {expression}")


class TestAgentlessWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = template("main_workflow.yaml")
        cls.role = template("datadog_integration_role.yaml")

    def test_saas_permissions_are_attached_to_the_created_role(self):
        policy = self.role["Resources"]["DatadogAgentlessSaaSScanningPolicy"]
        self.assertEqual(policy["Properties"]["Roles"], [{"Ref": "DatadogIntegrationRole"}])
        conditions = self.role["Conditions"]
        for enabled in ("true", "false"):
            with self.subTest(agentless=enabled):
                parameters = {"AgentlessScanning": enabled}
                self.assertEqual(evaluate(conditions[policy["Condition"]], parameters), enabled == "true")

    def test_role_stack_and_completion_callbacks_receive_agentless_setting(self):
        resources = self.workflow["Resources"]
        role_parameters = resources["DatadogIntegrationRoleStack"]["Properties"]["Parameters"]
        self.assertEqual(role_parameters["AgentlessScanning"], {"Ref": "AgentlessScanning"})
        for name in ("NotifyStackCompleteWithForwarder", "NotifyStackCompleteWithoutForwarder"):
            with self.subTest(callback=name):
                metadata = resources[name]["Properties"]["Metadata"]
                self.assertEqual(metadata["agentless_scanning"], {"Ref": "AgentlessScanning"})


if __name__ == "__main__":
    unittest.main()
