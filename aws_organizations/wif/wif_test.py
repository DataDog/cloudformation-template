"""Offline contract tests: real SigV4 signing, real inline handlers, fake AWS/DD APIs."""

import base64
from copy import deepcopy
from datetime import datetime, timezone
import fnmatch
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import urllib.parse

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
import yaml

CFN = types.ModuleType("cfnresponse")
CFN.SUCCESS, CFN.FAILED = "SUCCESS", "FAILED"
CFN.send = MagicMock()
sys.modules["cfnresponse"] = CFN
import wif_auth
import wif_bootstrap

SOURCE_ROOT = Path(__file__).resolve().parent
ROOT = SOURCE_ROOT.parent
ORG = "11111111-2222-4333-8444-555555555555"
SERVICE_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BROKER_ACCOUNT = "123456789012"
BROKER_ARN = f"arn:aws:iam::{BROKER_ACCOUNT}:role/DatadogWifBroker"
PAT = "test-only-bootstrap-pat"
AWS_CREDS = {"AccessKeyId": "ASIA" + "A" * 16, "SecretAccessKey": "test-only-aws-secret", "SessionToken": "test-only-aws-session"}
DD_ACCOUNT = "464622532012"


class CloudFormationLoader(yaml.SafeLoader):
    pass


def intrinsic(loader, suffix, node):
    key = suffix if suffix == "Ref" else "Fn::" + suffix
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
        if suffix == "GetAtt":
            value = value.split(".", 1)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {key: value}


CloudFormationLoader.add_multi_constructor("!", intrinsic)


def template(filename):
    return yaml.load((ROOT / filename).read_text(), Loader=CloudFormationLoader)


def bootstrap_event():
    return {
        "RequestType": "Create", "ResourceType": "Custom::DatadogStackSetWif",
        "ResourceProperties": {
            "DatadogSite": "datad0g.com", "DatadogOrgUUID": ORG,
            "ServiceAccountId": SERVICE_ACCOUNT, "WifBrokerRoleArn": BROKER_ARN,
            "ExpectedAccountId": BROKER_ACCOUNT, "AccountId": BROKER_ACCOUNT,
            "BootstrapPAT": PAT,
        },
    }


class Response(io.BytesIO):
    def __init__(self, data, code=200):
        super().__init__(json.dumps(data).encode() if data is not None else b"")
        self.code = code

    def getcode(self):
        return self.code


class FakeDatadog:
    def __init__(self):
        self.now = 1800000000
        self.pat_expires = self.now + 86400
        self.requests = []
        self.mappings = []
        self.tokens = {}
        self.exchange_count = 0
        self.pat_after_expiry = 0
        self.user_org = ORG
        self.service_account = True
        self.permissions = set(wif_bootstrap._REQUIRED_PERMISSIONS)
        self.accounts = {BROKER_ACCOUNT: self.account(BROKER_ACCOUNT)}

    @staticmethod
    def account(account):
        return {"id": "integration-" + account, "attributes": {
            "aws_account_id": account,
            "auth_config": {"role_name": "ExistingDatadogIntegrationRole", "external_id": "external-" + account},
        }}

    def exchange(self, authorization):
        if not self.mappings:
            raise wif_auth.DatadogHttpError(403, "/api/v2/delegated-token")
        assert authorization.startswith("Delegated ")
        body64, headers64, method, url64 = authorization[len("Delegated "):].split("|")
        body = base64.b64decode(body64).decode()
        headers = {k: v[0] for k, v in json.loads(base64.b64decode(headers64)).items()}
        url = base64.b64decode(url64).decode()
        assert method == "POST" and body == "Action=GetCallerIdentity&Version=2011-06-15"
        assert url == "https://sts.us-east-1.amazonaws.com/"
        assert headers["Host"] == "sts.us-east-1.amazonaws.com"
        assert headers["X-Ddog-Org-Id"] == ORG
        signature = headers.pop("Authorization")
        assert "x-ddog-org-id" in signature
        request = AWSRequest(method=method, url=url, data=body, headers=headers)
        request.context["timestamp"] = headers["X-Amz-Date"]
        signer = SigV4Auth(Credentials(AWS_CREDS["AccessKeyId"], AWS_CREDS["SecretAccessKey"], AWS_CREDS["SessionToken"]), "sts", "us-east-1")
        expected = signer.signature(signer.string_to_sign(request, signer.canonical_request(request)), request)
        assert signature.endswith("Signature=" + expected), "STS proof signature is invalid"
        self.exchange_count += 1
        token = f"test-wif-jwt-{self.exchange_count}"
        self.tokens[token] = self.now + 900
        return {"data": {"type": "token", "attributes": {"access_token": token, "expires": str(self.now + 900)}}}

    def request(self, site, path, headers, method="GET", body=None):
        self.requests.append((path, method, deepcopy(headers), deepcopy(body)))
        auth = headers.get("Authorization", "")
        if path == "/api/v2/delegated-token":
            return self.exchange(auth)
        if auth == "Bearer " + PAT:
            if self.now >= self.pat_expires:
                self.pat_after_expiry += 1
                raise wif_auth.DatadogHttpError(401, path)
        elif auth.startswith("Bearer "):
            if self.tokens.get(auth[7:], 0) <= self.now:
                raise wif_auth.DatadogHttpError(401, path)
        else:
            raise AssertionError("A WIF request tried to use static API/application keys")
        if path == f"/api/v2/users/{SERVICE_ACCOUNT}":
            return {"data": {"id": SERVICE_ACCOUNT, "attributes": {
                "service_account": self.service_account, "disabled": False, "handle": "service-account-handle",
            }, "relationships": {"org": {"data": {"id": self.user_org, "type": "orgs"}}}}}
        if path == f"/api/v2/users/{SERVICE_ACCOUNT}/permissions":
            return {"data": [{"attributes": {"name": name}} for name in self.permissions]}
        if path == "/api/v2/cloud_auth/aws/persona_mapping":
            if method == "POST":
                assert body["data"]["type"] == "aws_cloud_auth_config"
                attrs = body["data"]["attributes"]
                assert attrs == {"arn_pattern": f"arn:aws:sts::{BROKER_ACCOUNT}:assumed-role/DatadogWifBroker/*", "account_identifier": "service-account-handle"}
                mapping = {"id": "11111111-1111-4111-8111-111111111111", "attributes": {**attrs, "account_uuid": SERVICE_ACCOUNT}}
                self.mappings.append(mapping)
                return {"data": mapping}
            return {"data": deepcopy(self.mappings)}
        if path.startswith("/api/v2/integration/aws/accounts"):
            if method == "GET":
                account = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["aws_account_id"][0]
                return {"data": [self.accounts[account]] if account in self.accounts else []}
            if method == "POST":
                account = body["data"]["attributes"]["aws_account_id"]
                self.accounts[account] = self.account(account)
                return {"data": self.accounts[account]}
            account = path.rsplit("/integration-", 1)[1]
            if method == "PATCH":
                return {"data": self.accounts[account]}
            if method == "DELETE":
                self.accounts.pop(account, None)
                return None
        raise AssertionError((path, method))

    def urlopen(self, request, **_kwargs):
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path + ("?" + parsed.query if parsed.query else "")
        data = json.loads(request.data) if request.data else None
        return Response(self.request(parsed.hostname.removeprefix("api."), path, dict(request.header_items()), request.get_method(), data))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeDatadog()
        self.physical_ids = {}
        self.aws = MagicMock()
        self.aws.assume_role.return_value = {"Credentials": AWS_CREDS}
        self.aws.get_role.return_value = {"Role": {"AssumeRolePolicyDocument": {"Statement": [{
            "Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"AWS": f"arn:aws:iam::{DD_ACCOUNT}:root"},
            "Condition": {"StringEquals": {"sts:ExternalId": "external-" + BROKER_ACCOUNT}},
        }]}}}
        self.patches = [
            patch("boto3.client", return_value=self.aws),
            patch.dict("os.environ", {"AWS_REGION": "us-east-1"}),
            patch("time.time", side_effect=lambda: self.backend.now),
            patch("botocore.auth.get_current_datetime", side_effect=lambda: datetime.fromtimestamp(self.backend.now, timezone.utc)),
            patch.object(wif_auth, "wif_api_request", side_effect=self.backend.request),
            patch.object(wif_bootstrap, "wif_api_request", side_effect=self.backend.request),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        wif_auth._WIF_TOKEN_CACHE.clear()
        CFN.send.reset_mock()

    def worker(self):
        source = template("main_organizations.yaml")["Resources"]["DatadogAPICallFunction"]["Properties"]["Code"]["ZipFile"]
        namespace = {}
        with patch("signal.signal"):
            exec(compile(source, "inline-stackset-handler", "exec"), namespace)
        namespace["wif_api_request"] = self.backend.request
        return namespace

    def invoke_worker(self, worker, action, account="222222222222"):
        event = {"RequestType": action, "ResourceProperties": {
            "AuthenticationMode": "wif", "DatadogOrgUUID": ORG, "WifBrokerRoleArn": BROKER_ARN,
            "ApiURL": "datad0g.com", "AccountId": account, "RoleName": "DatadogIntegrationRole",
            "AWSPartition": "aws", "AccountTags": [], "CloudSecurityPostureManagement": "false",
            "DisableMetricCollection": "false", "DisableResourceCollection": "false",
        }}
        if account in self.physical_ids:
            event["PhysicalResourceId"] = self.physical_ids[account]
        CFN.send.reset_mock()
        opener = MagicMock()
        opener.open.side_effect = self.backend.urlopen
        with patch("urllib.request.urlopen", side_effect=self.backend.urlopen), patch("urllib.request.build_opener", return_value=opener):
            worker["handler"](event, object())
        args, kwargs = CFN.send.call_args
        if kwargs.get("physicalResourceId"):
            self.physical_ids[account] = kwargs["physicalResourceId"]
        return kwargs.get("responseStatus", args[2] if len(args) > 2 else None)

    def test_bootstrap_then_new_accounts_updates_and_deletion_after_pat_expiry(self):
        event = bootstrap_event()
        result, physical = wif_bootstrap.handle(event)
        self.assertEqual(result["DatadogTrustedAccountId"], DD_ACCOUNT)
        self.assertEqual(len(self.backend.mappings), 1)
        self.backend.now += 2 * 86400
        # A fresh Lambda in an account added two days later has no bootstrap PAT.
        worker = self.worker()
        self.assertEqual(self.invoke_worker(worker, "Create"), "SUCCESS")
        self.assertEqual(self.invoke_worker(worker, "Update"), "SUCCESS")
        self.assertEqual(self.invoke_worker(worker, "Delete"), "SUCCESS")
        self.assertEqual(self.invoke_worker(worker, "Delete"), "SUCCESS")
        # Ordinary bootstrap updates and deletion need no Datadog credential.
        update = {**event, "RequestType": "Update", "PhysicalResourceId": physical, "OldResourceProperties": deepcopy(event["ResourceProperties"])}
        self.assertEqual(wif_bootstrap.handle(update), (result, physical))
        wif_bootstrap.handle({**update, "RequestType": "Delete"})
        self.assertEqual(len(self.backend.mappings), 1)
        self.assertEqual(self.backend.pat_after_expiry, 0)
        self.assertGreaterEqual(self.backend.exchange_count, 2)

    def test_failed_creation_and_rollback_never_adopt_or_delete_an_existing_integration(self):
        wif_bootstrap.handle(bootstrap_event())
        account = "222222222222"
        existing = self.backend.account(account)
        self.backend.accounts[account] = existing
        worker = self.worker()
        self.assertEqual(self.invoke_worker(worker, "Create", account), "FAILED")
        self.assertEqual(self.invoke_worker(worker, "Delete", account), "SUCCESS")
        self.assertEqual(self.backend.accounts[account], existing)

    def test_token_refresh_and_no_fallback_after_mapping_revocation(self):
        wif_bootstrap.handle(bootstrap_event())
        worker = self.worker()
        self.assertEqual(self.invoke_worker(worker, "Create"), "SUCCESS")
        exchanges = self.backend.exchange_count
        self.assertEqual(self.invoke_worker(worker, "Update"), "SUCCESS")
        self.assertEqual(self.backend.exchange_count, exchanges)
        self.backend.now += 900
        self.assertEqual(self.invoke_worker(worker, "Update"), "SUCCESS")
        self.assertGreater(self.backend.exchange_count, exchanges)
        self.backend.mappings.clear()
        self.backend.now += 2 * 86400
        self.assertEqual(self.invoke_worker(worker, "Update"), "FAILED")
        self.assertEqual(self.backend.pat_after_expiry, 0)

    def test_retried_bootstrap_reuses_exact_mapping(self):
        first = wif_bootstrap.handle(bootstrap_event())
        self.assertEqual(wif_bootstrap.handle(bootstrap_event()), first)
        self.assertEqual(len(self.backend.mappings), 1)

    def test_conflicting_mapping_is_not_adopted_or_overwritten(self):
        wif_bootstrap.handle(bootstrap_event())
        self.backend.mappings[0]["attributes"]["account_uuid"] = "another-user"
        with self.assertRaisesRegex(wif_auth.WifError, "conflicting"):
            wif_bootstrap.handle(bootstrap_event())
        self.assertEqual(self.backend.mappings[0]["attributes"]["account_uuid"], "another-user")

    def test_missing_integration_human_account_wrong_org_and_missing_permissions_fail_closed(self):
        for change in ["integration", "human", "org", "permissions"]:
            with self.subTest(change=change):
                original = deepcopy(self.backend.__dict__)
                if change == "integration":
                    self.backend.accounts.clear()
                elif change == "human":
                    self.backend.service_account = False
                elif change == "org":
                    self.backend.user_org = "another-org"
                else:
                    self.backend.permissions.remove("aws_configuration_edit")
                with self.assertRaises(wif_auth.WifError):
                    wif_bootstrap.handle(bootstrap_event())
                self.assertEqual(self.backend.mappings, [])
                self.backend.__dict__ = original

    def test_binding_changes_require_explicit_replacement_not_an_expired_pat(self):
        event = bootstrap_event()
        _, physical = wif_bootstrap.handle(event)
        old = deepcopy(event["ResourceProperties"])
        event["ResourceProperties"]["ServiceAccountId"] = "new-target"
        with self.assertRaisesRegex(wif_auth.WifError, "immutable"):
            wif_bootstrap.handle({**event, "RequestType": "Update", "PhysicalResourceId": physical, "OldResourceProperties": old})

    def test_rejected_binding_update_can_roll_back_without_the_pat(self):
        original = bootstrap_event()
        result, physical = wif_bootstrap.handle(original)
        changed = deepcopy(original["ResourceProperties"])
        changed["ServiceAccountId"] = "another-target"
        self.backend.now += 2 * 86400
        failed = {**original, "RequestType": "Update", "PhysicalResourceId": physical, "ResourceProperties": changed}
        wif_bootstrap.handler(failed, object())
        self.assertEqual(CFN.send.call_args.args[2], "FAILED")
        self.assertEqual(CFN.send.call_args.kwargs["physicalResourceId"], physical)
        rollback = {**original, "RequestType": "Update", "PhysicalResourceId": physical, "OldResourceProperties": changed}
        self.assertEqual(wif_bootstrap.handle(rollback), (result, physical))
        self.assertEqual(self.backend.pat_after_expiry, 0)

    def test_worker_preserves_ownership_on_failed_updates_and_never_modifies_a_replacement(self):
        wif_bootstrap.handle(bootstrap_event())
        worker = self.worker()
        account = "222222222222"
        self.assertEqual(self.invoke_worker(worker, "Create", account), "SUCCESS")
        physical = self.physical_ids[account]
        old_id = self.backend.accounts[account]["id"]
        self.backend.accounts[account]["id"] = "replacement-integration"
        self.assertEqual(self.invoke_worker(worker, "Update", account), "FAILED")
        self.assertEqual(self.physical_ids[account], physical)
        self.assertEqual(self.backend.accounts[account]["id"], "replacement-integration")
        self.backend.accounts[account]["id"] = old_id
        self.assertEqual(self.invoke_worker(worker, "Update", account), "SUCCESS")

    def test_iam_propagation_retry_is_bounded_and_never_uses_the_pat(self):
        wif_bootstrap.handle(bootstrap_event())
        wif_auth._WIF_TOKEN_CACHE.clear()
        self.aws.assume_role.side_effect = [ClientError({"Error": {"Code": "AccessDenied"}}, "AssumeRole"), {"Credentials": AWS_CREDS}]
        with patch("time.sleep") as sleep:
            wif_auth.WifClient("datad0g.com", ORG, BROKER_ARN).headers()
        sleep.assert_called_once_with(1)

    def test_ambiguous_or_broadened_integration_trust_is_not_copied(self):
        statement = self.aws.get_role.return_value["Role"]["AssumeRolePolicyDocument"]["Statement"][0]
        statement["Principal"]["AWS"] = "arn:aws:iam::464622532012:role/SpecificRole"
        with self.assertRaisesRegex(wif_auth.WifError, "account root"):
            wif_bootstrap.handle(bootstrap_event())
        self.assertEqual(self.backend.mappings, [])

    def test_invalid_origins_orgs_and_broker_patterns_do_not_contact_aws(self):
        for site, org, role in [("attacker.example", ORG, BROKER_ARN), ("datad0g.com", "invalid", BROKER_ARN), ("datad0g.com", ORG, "arn:aws:iam::*:role/Worker")]:
            with self.assertRaises(wif_auth.WifError):
                wif_auth.WifClient(site, org, role)
        self.aws.assume_role.assert_not_called()

    def test_legacy_headers_are_unchanged_and_unknown_mode_is_rejected(self):
        self.assertEqual(wif_auth.datadog_stackset_headers({"APIKey": "api", "APPKey": "app"}), {"DD-API-KEY": "api", "DD-APPLICATION-KEY": "app"})
        with self.assertRaises(wif_auth.WifError):
            wif_auth.datadog_stackset_headers({"AuthenticationMode": "unknown"})


class TemplateTests(unittest.TestCase):
    def test_inline_sources_are_synchronized(self):
        subprocess.run([sys.executable, str(SOURCE_ROOT / "render_wif.py"), "--check"], check=True)

    def test_regeneration_uses_sibling_templates_from_any_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "aws_organizations"
            sources = root / "wif"
            sources.mkdir(parents=True)
            names = ["main_organizations.yaml", "main_organizations_wif.yaml"]
            for name in names:
                shutil.copyfile(ROOT / name, root / name)
            for name in ["render_wif.py", "wif_auth.py", "wif_bootstrap.py"]:
                shutil.copyfile(SOURCE_ROOT / name, sources / name)
            auth = sources / "wif_auth.py"
            auth.write_text(auth.read_text() + "\n# Test-only source change\n")
            before = {name: (root / name).read_bytes() for name in names}
            command = [sys.executable, str(sources / "render_wif.py")]
            stale = subprocess.run(command + ["--check"], cwd=temp, capture_output=True, text=True)
            self.assertEqual(stale.returncode, 1)
            self.assertIn("wif/render_wif.py", stale.stderr)
            for name in names:
                self.assertEqual((root / name).read_bytes(), before[name])
            subprocess.run(command, cwd=temp, check=True)
            subprocess.run(command + ["--check"], cwd=sources, check=True)
            for name in names:
                self.assertIn("# Test-only source change", (root / name).read_text())
            self.assertFalse(list(sources.glob("*.yaml")))

    def test_release_output_cannot_overwrite_either_source_directory(self):
        for output in [ROOT, SOURCE_ROOT]:
            with self.subTest(output=output):
                result = subprocess.run([
                    sys.executable, str(SOURCE_ROOT / "render_wif.py"),
                    "--output", str(output), "--bucket", "test-wif-artifacts", "--version", "v4.4.0",
                ], capture_output=True, text=True)
                self.assertEqual(result.returncode, 2)
                self.assertIn("must not overwrite the source directory", result.stderr)

    def test_no_bootstrap_credential_reaches_member_accounts(self):
        root = template("main_organizations_wif.yaml")
        self.assertTrue(root["Parameters"]["BootstrapPAT"]["NoEcho"])
        stackset = root["Resources"]["IntegrationStackSet"]["Properties"]
        names = {p["ParameterKey"] for p in stackset["Parameters"]}
        self.assertTrue({"AuthenticationMode", "DatadogOrgUUID", "WifBrokerRoleArn", "WifWorkerRoleName"} <= names)
        self.assertFalse({"BootstrapPAT", "DatadogApiKey", "DatadogAppKey"} & names)
        self.assertEqual(root["Resources"]["WifMapping"]["DeletionPolicy"], "Retain")
        self.assertEqual(stackset["StackInstancesGroup"][0]["Regions"], [{"Ref": "AWS::Region"}])

    def test_broker_trust_requires_both_organization_and_exact_worker_role_name(self):
        root = template("main_organizations_wif.yaml")
        statements = root["Resources"]["WifBrokerRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        worker = next(s for s in statements if s["Sid"] == "OrganizationStackSetWorkers")
        self.assertIn("aws:PrincipalOrgID", worker["Condition"]["StringEquals"])
        self.assertIn("aws:PrincipalOrgPaths", worker["Condition"]["ForAnyValue:StringLike"])
        arn = worker["Condition"]["ArnLike"]["aws:PrincipalArn"]["Fn::Sub"][0]
        self.assertEqual(arn, "arn:aws:iam::*:role/DatadogWif-${StackUuid}")
        self.assertNotIn("Policies", root["Resources"]["WifBrokerRole"]["Properties"])

    def test_organization_paths_are_scoped_and_target_changes_can_roll_back(self):
        code = template("main_organizations_wif.yaml")["Resources"]["OrganizationInfoFunction"]["Properties"]["Code"]["ZipFile"]
        module = {}
        exec(compile(code, "inline-organization-info", "exec"), module)
        aws = MagicMock()
        aws.describe_organization.return_value = {"Organization": {"Id": "o-1234567890", "MasterAccountId": BROKER_ACCOUNT}}
        props = {"AccountId": BROKER_ACCOUNT, "CallAs": "SELF", "OrganizationalUnitIds": ["ou-abcd-12345678"]}
        event = {"RequestType": "Create", "ResourceProperties": props}
        with patch("boto3.client", return_value=aws):
            module["handler"](event, object())
            data = CFN.send.call_args.args[3]
            physical = CFN.send.call_args.kwargs["physicalResourceId"]
            pattern = data["OrganizationPaths"]
            self.assertTrue(fnmatch.fnmatchcase("o-1234567890/r-abcd/ou-abcd-12345678/", pattern))
            self.assertTrue(fnmatch.fnmatchcase("o-1234567890/r-abcd/ou-abcd-12345678/ou-efgh-87654321/", pattern))
            self.assertFalse(fnmatch.fnmatchcase("o-1234567890/r-abcd/ou-abcd-99999999/", pattern))
            changed = {**props, "OrganizationalUnitIds": ["ou-abcd-99999999"]}
            module["handler"]({**event, "RequestType": "Update", "PhysicalResourceId": physical, "ResourceProperties": changed}, object())
            self.assertEqual(CFN.send.call_args.args[2], "FAILED")
            self.assertEqual(CFN.send.call_args.kwargs["physicalResourceId"], physical)
            module["handler"]({**event, "RequestType": "Update", "PhysicalResourceId": physical, "OldResourceProperties": changed}, object())
            self.assertEqual(CFN.send.call_args.args[2], "SUCCESS")

    def test_release_rendering_substitutes_both_templates_without_mutating_sources(self):
        names = ["main_organizations.yaml", "main_organizations_wif.yaml"]
        before = {name: (ROOT / name).read_bytes() for name in names}
        with tempfile.TemporaryDirectory() as output:
            subprocess.run([sys.executable, str(SOURCE_ROOT / "render_wif.py"), "--output", output, "--bucket", "test-wif-artifacts", "--version", "v4.4.0"], cwd=output, check=True)
            for name in names:
                rendered = (Path(output) / name).read_text()
                self.assertNotIn("<BUCKET_PLACEHOLDER>", rendered)
                self.assertNotIn("<VERSION_PLACEHOLDER>", rendered)
                self.assertEqual((ROOT / name).read_bytes(), before[name])
            self.assertIn("https://test-wif-artifacts.s3.amazonaws.com/aws/v4.4.0/main_organizations.yaml", (Path(output) / names[1]).read_text())

    def test_worker_role_changes_wait_until_integration_creation_succeeds(self):
        resources = template("main_organizations.yaml")["Resources"]
        self.assertEqual(resources["DatadogAttachIntegrationPermissionsFunctionTrigger"]["DependsOn"], "DatadogIntegrationRole")
        trust = resources["DatadogIntegrationRole"]["Properties"]["AssumeRolePolicyDocument"]
        self.assertEqual(trust["Statement"][0]["Condition"]["StringEquals"]["sts:ExternalId"], {"Fn::GetAtt": ["DatadogAWSAccountIntegration", "ExternalId"]})

    def test_bootstrap_dependencies_are_acyclic(self):
        resources = template("main_organizations_wif.yaml")["Resources"]
        graph = {}

        def references(value):
            found = set()
            if isinstance(value, dict):
                if value.get("Ref") in resources:
                    found.add(value["Ref"])
                if "Fn::GetAtt" in value:
                    found.add(value["Fn::GetAtt"][0])
                for child in value.values():
                    found |= references(child)
            elif isinstance(value, list):
                for child in value:
                    found |= references(child)
            return found

        for name, resource in resources.items():
            deps = resource.get("DependsOn", [])
            graph[name] = references(resource) | ({deps} if isinstance(deps, str) else set(deps))
        visited = set()

        def visit(name, parents):
            self.assertNotIn(name, parents, f"circular CloudFormation dependency: {name}")
            if name in visited:
                return
            for dependency in graph[name]:
                visit(dependency, parents | {name})
            visited.add(name)

        for name in graph:
            visit(name, set())
        self.assertIn("WifBrokerRole", graph["IntegrationStackSet"])
        self.assertIn("WifMapping", graph["IntegrationStackSet"])


class HttpSafetyTests(unittest.TestCase):
    def test_worker_api_requests_also_disable_redirects(self):
        request = urllib.request.Request("https://api.datad0g.com/api/v2/integration/aws/accounts", headers={"Authorization": "Bearer test-jwt"})
        with patch("urllib.request.build_opener") as build, patch("urllib.request.urlopen") as legacy:
            wif_auth.datadog_stackset_urlopen(request, {"AuthenticationMode": "wif"})
            self.assertIsInstance(build.call_args.args[0], wif_auth._NoCredentialRedirect)
            build.return_value.open.assert_called_once_with(request, timeout=15)
            legacy.assert_not_called()

    def test_http_errors_and_redirects_do_not_expose_credentials(self):
        response = urllib.error.HTTPError("https://api.datad0g.com/api/v2/example", 302, "redirect " + PAT, {}, io.BytesIO(PAT.encode()))
        opener = MagicMock()
        opener.open.side_effect = response
        with patch("urllib.request.build_opener", return_value=opener) as build:
            with self.assertRaises(wif_auth.DatadogHttpError) as error:
                wif_auth.wif_api_request("datad0g.com", "/api/v2/example", {"Authorization": "Bearer " + PAT})
        self.assertNotIn(PAT, str(error.exception))
        self.assertIsInstance(build.call_args.args[0], wif_auth._NoCredentialRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://attacker.example"))


if __name__ == "__main__":
    unittest.main()
