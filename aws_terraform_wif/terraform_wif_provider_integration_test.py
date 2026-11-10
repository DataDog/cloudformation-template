"""Opt-in test using released Terraform providers and loopback-only fake services.

Set TERRAFORM_WIF_INTEGRATION_BINARY to a Terraform >=1.10 binary. Set
TERRAFORM_WIF_PROVIDER_MIRROR to an existing .terraform/providers directory to
avoid downloading providers. No AWS or Datadog credentials/resources are used.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
import urllib.request

import terraform_wif_bootstrap as bootstrap

BINARY = os.environ.get("TERRAFORM_WIF_INTEGRATION_BINARY")
MIRROR = os.environ.get("TERRAFORM_WIF_PROVIDER_MIRROR")
ORG = "11111111-2222-4333-8444-555555555555"
USER = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT = "123456789012"
PAT = "test-bootstrap-pat-not-a-real-credential"
KEY = "ASIA" + "A" * 16
SECRET = "test-aws-secret-not-a-real-credential"
SESSION = "test-aws-session"
ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/TerraformRunner/provider-session"
INTEGRATION = "22222222-3333-4333-8333-444444444444"


class Backend:
    def __init__(self):
        self.account = None
        self.mapping = None
        self.pat_valid = True
        self.expired_pat_attempts = 0
        self.calls = []
        self.external = []
        self.tokens = set()

    def proof(self, value):
        prefix, payload = value.split(" ", 1)
        assert prefix in {"Delegated", "Bearer"}
        body64, headers64, method, url64 = payload.split("|")
        headers = {k.lower(): v[0] for k, v in json.loads(base64.b64decode(headers64)).items()}
        body = base64.b64decode(body64)
        url = urllib.parse.urlsplit(base64.b64decode(url64).decode())
        assert headers["x-ddog-org-id"] == ORG
        assert headers["x-amz-security-token"] == SESSION
        assert method == "POST" and b"Action=GetCallerIdentity" in body
        assert url.hostname in {"sts.amazonaws.com", "sts.us-east-1.amazonaws.com"}
        auth = headers["authorization"]
        credential = auth.split("Credential=", 1)[1].split(",", 1)[0]
        access, date, region, service, terminator = credential.split("/")
        assert access == KEY and service == "sts" and terminator == "aws4_request"
        signed = auth.split("SignedHeaders=", 1)[1].split(",", 1)[0]
        assert "x-ddog-org-id" in signed.split(";")
        canonical_headers = "".join(name + ":" + " ".join(headers[name].split()) + "\n" for name in signed.split(";"))
        canonical = "\n".join((method, url.path or "/", url.query, canonical_headers, signed, hashlib.sha256(body).hexdigest()))
        scope = "/".join((date, region, service, terminator))
        to_sign = "\n".join(("AWS4-HMAC-SHA256", headers["x-amz-date"], scope, hashlib.sha256(canonical.encode()).hexdigest()))
        sign = lambda key, data: hmac.new(key, data.encode(), hashlib.sha256).digest()
        key = sign(sign(sign(sign(("AWS4" + SECRET).encode(), date), region), service), terminator)
        assert auth.endswith("Signature=" + hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest())


def handler_for(backend):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, body):
            raw = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_CONNECT(self):
            backend.external.append(self.path)
            self.send_error(502, "External network disabled for this test")

        def do_POST(self):
            self.handle_request()

        def do_GET(self):
            self.handle_request()

        def do_PATCH(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/" and b"Action=GetCallerIdentity" in raw:
                xml = f'<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><GetCallerIdentityResult><Arn>{ARN}</Arn><UserId>AROAEXAMPLE:provider-session</UserId><Account>{ACCOUNT}</Account></GetCallerIdentityResult><ResponseMetadata><RequestId>test-request</RequestId></ResponseMetadata></GetCallerIdentityResponse>'.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(xml)))
                self.end_headers()
                self.wfile.write(xml)
                return
            path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
            # The published framework provider sends JSON-quoted UUIDs. The
            # real API accepts them (also covered by its recorded acceptance tests).
            path = path.replace('/"' + INTEGRATION + '"', '/' + INTEGRATION)
            auth = self.headers.get("Authorization", "")
            if path == "/api/v2/delegated-token":
                if not backend.mapping:
                    self.send(403, {"errors": ["No mapping"]})
                    return
                try:
                    backend.proof(auth)
                except Exception:
                    self.send(400, {"errors": ["Invalid signed proof"]})
                    return
                token = f"test-delegated-token-{len(backend.tokens)}"
                backend.tokens.add(token)
                backend.calls.append(("exchange", path))
                self.send(200, {"data": {"id": "token-id", "type": "token", "attributes": {"access_token": token, "expires": str(int(time.time()) + 900)}}})
                return
            using_pat = auth == "Bearer " + PAT or self.headers.get("DD-APPLICATION-KEY") == PAT
            using_wif = auth.removeprefix("Bearer ") in backend.tokens
            backend.calls.append(("pat" if using_pat else "wif" if using_wif else "unknown", path))
            if using_pat and not backend.pat_valid:
                backend.expired_pat_attempts += 1
            if using_pat and not backend.pat_valid or not (using_pat or using_wif):
                self.send(401, {"errors": ["Unauthorized"]})
                return
            if path == "/api/v2/validate_keys":
                self.send(200, {"status": "ok"})
            elif path == f"/api/v2/users/{USER}":
                self.send(200, {"data": {"id": USER, "type": "users", "attributes": {"handle": "automation", "service_account": True, "disabled": False}, "relationships": {"org": {"data": {"id": ORG, "type": "orgs"}}}}})
            elif path == f"/api/v2/users/{USER}/permissions":
                self.send(200, {"data": [{"id": name, "type": "permissions", "attributes": {"name": name}} for name in bootstrap.REQUIRED_PERMISSIONS]})
            elif path == bootstrap.MAPPING_PATH:
                if self.command == "POST":
                    data = json.loads(raw)["data"]
                    assert data["type"] == "aws_cloud_auth_config"
                    assert data["attributes"]["arn_pattern"] == f"arn:aws:sts::{ACCOUNT}:assumed-role/TerraformRunner/*"
                    backend.mapping = {"id": "mapping-id", "type": "aws_cloud_auth_config", "attributes": {**data["attributes"], "account_uuid": USER}}
                    # Revoke the bootstrap PAT immediately: all later requests
                    # must succeed using the real provider's AWS WIF path.
                    backend.pat_valid = False
                    self.send(201, {"data": backend.mapping})
                else:
                    self.send(200, {"data": [backend.mapping] if backend.mapping else []})
            elif path == "/api/v2/integration/aws/iam_permissions":
                self.send(200, {"data": {"type": "permissions", "attributes": {"permissions": ["ec2:DescribeInstances"]}}})
            elif path == "/api/v2/integration/aws/accounts" and self.command == "GET":
                self.send(200, {"data": [backend.account] if backend.account else []})
            elif path == "/api/v2/integration/aws/accounts" and self.command == "POST":
                attrs = json.loads(raw)["data"]["attributes"]
                attrs["auth_config"]["external_id"] = "test-external-id"
                backend.account = {"id": INTEGRATION, "type": "account", "attributes": attrs}
                self.send(200, {"data": backend.account})
            elif path == "/api/v2/integration/aws/accounts/" + INTEGRATION:
                if self.command == "DELETE":
                    backend.account = None
                    self.send(204, None)
                elif backend.account is None:
                    self.send(404, {"errors": ["Not found"]})
                else:
                    if self.command == "PATCH":
                        attrs = json.loads(raw)["data"]["attributes"]
                        attrs["auth_config"]["external_id"] = "test-external-id"
                        backend.account["attributes"] = attrs
                    self.send(200, {"data": backend.account})
            else:
                self.send(404, {"errors": ["Unknown test path"]})
    return Handler


@unittest.skipUnless(BINARY and MIRROR, "Set TERRAFORM_WIF_INTEGRATION_BINARY and TERRAFORM_WIF_PROVIDER_MIRROR")
class ProviderIntegrationTests(unittest.TestCase):
    def test_published_provider_switches_from_pat_to_wif_and_destroys_after_pat_revocation(self):
        backend = Backend()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tf").write_text(f'''terraform {{
  required_version = ">= 1.10"
  required_providers {{
    aws = {{ source = "hashicorp/aws" }}
    datadog = {{ source = "DataDog/datadog", version = "= 4.29.0" }}
  }}
}}
variable "datadog_wif_bootstrap_pat" {{
  type = string
  sensitive = true
  ephemeral = true
  default = null
}}
provider "aws" {{
  region = "us-east-1"
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
  max_retries = 0
  endpoints {{ sts = "{origin}" }}
}}
provider "datadog" {{
  api_url = "{origin}"
  org_uuid = "{ORG}"
  cloud_provider_type = "aws"
  cloud_provider_region = "us-east-1"
  http_client_retry_enabled = "false"
}}
data "aws_caller_identity" "execution" {{}}
data "datadog_integration_aws_iam_permissions" "datadog_permissions" {{}}
resource "datadog_integration_aws_account" "datadog_integration" {{
  aws_account_id = "{ACCOUNT}"
  aws_partition = "aws"
  account_tags = []
  aws_regions {{ include_all = true }}
  auth_config {{
    aws_auth_config_role {{ role_name = "DatadogIntegrationRole" }}
  }}
  resources_config {{
    cloud_security_posture_management_collection = false
    extended_collection = false
  }}
  traces_config {{
    xray_services {{}}
  }}
  logs_config {{
    lambda_forwarder {{}}
  }}
  metrics_config {{
    enabled = false
    namespace_filters {{}}
  }}
  lifecycle {{
    precondition {{
      condition = data.aws_caller_identity.execution.account_id == "{ACCOUNT}"
      error_message = "Wrong AWS account"
    }}
  }}
}}
output "datadog_wif_bootstrap" {{
  value = {{
    contract_version = 1
    aws_account_id = data.aws_caller_identity.execution.account_id
    arn_pattern = "arn:aws:sts::{ACCOUNT}:assumed-role/TerraformRunner/*"
    integration_id = datadog_integration_aws_account.datadog_integration.id
    org_uuid = "{ORG}"
    site = "datad0g.com"
  }}
}}
''')
            config = root / "terraform.rc"
            config.write_text('provider_installation {\n  filesystem_mirror {\n    path = ' + json.dumps(str(Path(MIRROR).resolve())) + '\n  }\n}\n')
            fake_aws = root / "aws"
            fake_aws.write_text('#!' + os.sys.executable + '\nimport json\nprint(json.dumps(' + repr({"Arn": ARN, "Account": ACCOUNT}) + '))\n')
            fake_aws.chmod(0o700)
            aws_config = root / "aws-config"
            aws_config.write_text('[default]\nregion = us-east-1\n')
            env = {
                "PATH": os.environ.get("PATH", ""), "HOME": str(root), "TF_CLI_CONFIG_FILE": str(config), "CHECKPOINT_DISABLE": "1",
                "AWS_ACCESS_KEY_ID": KEY, "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_SESSION_TOKEN": SESSION,
                "AWS_REGION": "us-east-1", "AWS_DEFAULT_REGION": "us-east-1", "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_SHARED_CREDENTIALS_FILE": str(root / "no-credentials"), "AWS_CONFIG_FILE": str(aws_config),
                "HTTP_PROXY": origin, "HTTPS_PROXY": origin, "NO_PROXY": "127.0.0.1,localhost",
                "DD_API_KEY": "must-not-be-used", "DD_APP_KEY": "must-not-be-used", "DD_BEARER_TOKEN": PAT,
            }
            args = argparse.Namespace(site="datad0g.com", org_uuid=ORG, service_account_id=USER, account_id=ACCOUNT,
                                      directory=str(root), terraform=BINARY, aws=str(fake_aws), aws_profile=None, auto_approve=True)
            build = urllib.request.build_opener

            def local_opener(*handlers):
                delegate = build(*handlers)
                class LocalOnly:
                    def open(self, request, **kwargs):
                        self_url = urllib.parse.urlsplit(request.full_url)
                        assert self_url.hostname == "api.datad0g.com"
                        local = urllib.request.Request(origin + self_url.path + ("?" + self_url.query if self_url.query else ""), data=request.data, headers=dict(request.header_items()), method=request.method)
                        return delegate.open(local, **kwargs)
                return LocalOnly()

            run = bootstrap.run_command

            def audit_run(command, directory, environment, capture=False, allow_failure=False):
                if command[1] == "apply" and any(arg.startswith("-target=") for arg in command):
                    plan = root / "bootstrap-plan.tfplan"
                    run([BINARY, "plan", "-input=false", "-target=" + bootstrap.RESOURCE, "-out=" + str(plan)], directory, environment, capture=True)
                    plan_json = run([BINARY, "show", "-json", str(plan)], directory, environment, capture=True)
                    self.assertNotIn(PAT, plan_json.stdout)
                    plan.unlink()
                return run(command, directory, environment, capture=capture, allow_failure=allow_failure)

            with patch.dict(os.environ, env, clear=True), patch("urllib.request.build_opener", side_effect=local_opener), patch.object(bootstrap, "run_command", side_effect=audit_run):
                bootstrap.bootstrap(args, PAT)
                self.assertFalse(backend.pat_valid)
                self.assertFalse((root / bootstrap.OVERRIDE_NAME).exists())
                state = (root / "terraform.tfstate").read_text()
                self.assertNotIn(PAT, state)
                result = subprocess.run([BINARY, "destroy", "-auto-approve", "-no-color"], cwd=root, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr[-2500:])
                self.assertIsNone(backend.account, f"Unexpected resource lifecycle: {backend.calls}")
            self.assertTrue(any(kind == "exchange" for kind, _ in backend.calls))
            self.assertTrue(any(kind == "wif" and path.endswith(INTEGRATION) for kind, path in backend.calls))
            self.assertEqual(backend.expired_pat_attempts, 0)
            self.assertFalse(backend.external, f"Attempted external network access: {backend.external}")


if __name__ == "__main__":
    unittest.main()
