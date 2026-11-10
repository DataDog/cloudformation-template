import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import terraform_wif_bootstrap as bootstrap

ORG = "11111111-2222-4333-8444-555555555555"
USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT = "123456789012"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/TerraformRunner/session-one"
PAT = "test-only-bootstrap-pat"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.args = argparse.Namespace(site="datad0g.com", org_uuid=ORG, service_account_id=USER,
                                       account_id=ACCOUNT, directory=self.temp.name, terraform="terraform",
                                       aws="aws", aws_profile=None, auto_approve=False)
        self.outputs = None
        self.accounts = []
        self.mappings = []
        self.calls = []
        self.api_calls = []
        self.fail_apply = False
        self.fail_output = False
        self.fail_state = False
        self.fail_mapping = False
        self.aws_arn = ARN
        self.env = patch.dict(os.environ, {"DD_API_KEY": "old-api-key", "DD_APP_KEY": "old-app-key", "DD_BOOTSTRAP_PAT": PAT}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.command = patch.object(bootstrap, "run_command", side_effect=self.run_command)
        self.command.start()
        self.addCleanup(self.command.stop)
        self.api = patch.object(bootstrap, "api_request", side_effect=self.api_request)
        self.api.start()
        self.addCleanup(self.api.stop)

    def contract(self):
        return {"contract_version": 1, "aws_account_id": ACCOUNT, "org_uuid": ORG, "site": "datad0g.com",
                "arn_pattern": f"arn:aws:sts::{ACCOUNT}:assumed-role/TerraformRunner/*", "integration_id": "integration-id"}

    def run_command(self, command, directory, environment, capture=False, allow_failure=False):
        self.calls.append((list(command), dict(environment)))
        code, output, error = 0, "", ""
        if command[1] == "version":
            output = json.dumps({"terraform_version": "1.10.8"})
        elif command[0] == "aws":
            output = json.dumps({"Arn": self.aws_arn, "Account": ACCOUNT})
        elif command[1] == "output":
            if self.fail_output:
                code, error = 1, "Access denied to backend"
            elif self.outputs:
                output = json.dumps(self.outputs)
            else:
                code, error = 1, f'Output "{bootstrap.OUTPUT}" not found'
        elif command[1] == "state":
            code, error = 1, "Access denied" if self.fail_state else "No state file was found!"
        elif command[1] == "apply" and any(arg.startswith("-target=") for arg in command):
            self.assertEqual(environment[bootstrap.PAT_VARIABLE], PAT)
            override = self.directory / bootstrap.OVERRIDE_NAME
            self.assertIn("ephemeral = true", override.read_text())
            self.assertNotIn(PAT, override.read_text())
            self.assertEqual(override.stat().st_mode & 0o777, 0o600)
            if self.fail_apply:
                raise bootstrap.BootstrapError("terraform apply failed")
            self.outputs = self.contract()
            self.accounts = [{"id": "integration-id", "attributes": {"aws_account_id": ACCOUNT}}]
            (self.directory / "terraform.tfstate").write_text(json.dumps({"outputs": self.outputs}))
        elif command[1] in {"apply", "plan"}:
            self.assertNotIn(bootstrap.PAT_VARIABLE, environment)
            self.assertNotIn("DD_BOOTSTRAP_PAT", environment)
            self.assertNotIn("DD_API_KEY", environment)
            self.assertNotIn("DD_APP_KEY", environment)
            self.assertFalse((self.directory / bootstrap.OVERRIDE_NAME).exists())
            self.assertTrue(self.mappings)
        return subprocess.CompletedProcess(command, code, output, error)

    def api_request(self, site, pat, path, method="GET", body=None):
        self.api_calls.append((path, method, deepcopy(body)))
        self.assertEqual(pat, PAT)
        if path == f"/api/v2/users/{USER}":
            return {"data": {"id": USER, "attributes": {"service_account": True, "disabled": False, "handle": "automation-handle"},
                             "relationships": {"org": {"data": {"id": ORG}}}}}
        if path.endswith("/permissions"):
            return {"data": [{"attributes": {"name": name}} for name in bootstrap.REQUIRED_PERMISSIONS]}
        if path.startswith("/api/v2/integration/aws/accounts"):
            return {"data": self.accounts}
        if path == bootstrap.MAPPING_PATH:
            if method == "GET":
                return {"data": self.mappings}
            if self.fail_mapping:
                raise bootstrap.ApiError(403, path)
            self.assertEqual(body["data"]["type"], "aws_cloud_auth_config")
            self.assertEqual(body["data"]["attributes"]["arn_pattern"], self.contract()["arn_pattern"])
            self.assertEqual(body["data"]["attributes"]["account_identifier"], "automation-handle")
            mapping = {"id": "mapping-id", "attributes": {**body["data"]["attributes"], "account_uuid": USER}}
            self.mappings.append(mapping)
            return {"data": mapping}
        raise AssertionError(path)

    def test_two_phase_bootstrap_removes_pat_override_before_wif_operations(self):
        self.assertEqual(bootstrap.bootstrap(self.args, PAT), "mapping-id")
        applies = [(command, env) for command, env in self.calls if command[1] == "apply"]
        self.assertEqual(len(applies), 2)
        self.assertIn("-target=" + bootstrap.RESOURCE, applies[0][0])
        self.assertNotIn("-auto-approve", applies[0][0])
        self.assertNotIn(bootstrap.PAT_VARIABLE, applies[1][1])
        self.assertNotIn(PAT, (self.directory / "terraform.tfstate").read_text())
        self.assertFalse((self.directory / bootstrap.OVERRIDE_NAME).exists())
        for command, _ in self.calls:
            self.assertNotIn(PAT, " ".join(command))

    def test_failed_first_apply_always_removes_override(self):
        self.fail_apply = True
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.bootstrap(self.args, PAT)
        self.assertFalse((self.directory / bootstrap.OVERRIDE_NAME).exists())
        self.assertEqual(self.mappings, [])

    def test_failed_mapping_leaves_owned_state_and_can_be_resumed_without_pat_provider(self):
        self.fail_mapping = True
        with self.assertRaises(bootstrap.ApiError):
            bootstrap.bootstrap(self.args, PAT)
        self.assertFalse((self.directory / bootstrap.OVERRIDE_NAME).exists())
        self.fail_mapping = False
        self.calls.clear()
        bootstrap.bootstrap(self.args, PAT)
        self.assertFalse(any(bootstrap.PAT_VARIABLE in env for _, env in self.calls))

    def test_existing_unowned_integration_is_not_adopted(self):
        self.accounts = [{"id": "other-id", "attributes": {"aws_account_id": ACCOUNT}}]
        with self.assertRaisesRegex(bootstrap.BootstrapError, "outside"):
            bootstrap.bootstrap(self.args, PAT)
        self.assertFalse(any(command[1] == "apply" for command, _ in self.calls))
        self.assertEqual(self.mappings, [])

    def test_backend_read_errors_do_not_start_fresh_registration(self):
        for failure in ["fail_output", "fail_state"]:
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                with self.assertRaisesRegex(bootstrap.BootstrapError, "state"):
                    bootstrap.bootstrap(self.args, PAT)
                self.assertFalse(any(command[1] == "apply" for command, _ in self.calls))
                setattr(self, failure, False)

    def test_existing_override_is_never_overwritten(self):
        file = self.directory / bootstrap.OVERRIDE_NAME
        file.write_text("user content")
        with self.assertRaisesRegex(bootstrap.BootstrapError, "override"):
            bootstrap.bootstrap(self.args, PAT)
        self.assertEqual(file.read_text(), "user content")

    def test_existing_matching_mapping_is_reused(self):
        bootstrap.bootstrap(self.args, PAT)
        self.calls.clear()
        bootstrap.bootstrap(self.args, PAT)
        self.assertEqual(len(self.mappings), 1)
        self.assertFalse(any(bootstrap.PAT_VARIABLE in env for _, env in self.calls))

    def test_unsafe_identity_and_cli_argument_overrides_fail_before_apply(self):
        self.aws_arn = f"arn:aws:iam::{ACCOUNT}:user/example"
        with self.assertRaisesRegex(bootstrap.BootstrapError, "assumed AWS role"):
            bootstrap.bootstrap(self.args, PAT)
        self.assertEqual(self.api_calls, [])
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.clean_environment({"TF_CLI_ARGS_apply": "-destroy -auto-approve"})

    def test_conflicting_mapping_is_not_replaced(self):
        self.mappings = [{"id": "existing", "attributes": {"arn_pattern": self.contract()["arn_pattern"], "account_uuid": "different-user"}}]
        with self.assertRaisesRegex(bootstrap.BootstrapError, "conflicting"):
            bootstrap.bootstrap(self.args, PAT)
        self.assertFalse(any(command[1] == "apply" for command, _ in self.calls))

    def test_session_names_do_not_change_the_role_mapping(self):
        self.assertEqual(bootstrap.role_pattern(ARN, ACCOUNT), bootstrap.role_pattern(ARN.replace("session-one", "session-two"), ACCOUNT))
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.role_pattern(ARN.replace("TerraformRunner", "DatadogIntegrationRole"), ACCOUNT)


if __name__ == "__main__":
    unittest.main()
