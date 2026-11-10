#!/usr/bin/env python3
"""Bootstrap the generated AWS Terraform configuration, then use WIF only.

Requires Terraform >=1.10, AWS CLI, and an assumed AWS role shared by the AWS
and Datadog providers. The PAT is read from a terminal or DD_BOOTSTRAP_PAT, never
from an argument. Its only provider use is an ephemeral variable in a temporary
override. No PAT is written to HCL, tfvars, state, plans, or launch URLs.
"""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

SITES = frozenset({
    "datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com",
    "ap1.datadoghq.com", "ap2.datadoghq.com", "uk1.datadoghq.com", "datad0g.com",
})
MAPPING_PATH = "/api/v2/cloud_auth/aws/persona_mapping"
RESOURCE = "datadog_integration_aws_account.datadog_integration"
OUTPUT = "datadog_wif_bootstrap"
OVERRIDE_NAME = "zz_datadog_wif_bootstrap_override.tf"
PAT_VARIABLE = "TF_VAR_datadog_wif_bootstrap_pat"
REQUIRED_PERMISSIONS = {"aws_configuration_read", "aws_configuration_edit", "aws_configurations_manage"}
OVERRIDE = '''# Temporary, credential-free bootstrap override. Removed before WIF operations.
variable "datadog_wif_bootstrap_pat" {
  type      = string
  sensitive = true
  ephemeral = true
}
provider "datadog" {
  cloud_provider_type   = null
  cloud_provider_region = null
  org_uuid              = null
  api_key               = null
  app_key               = null
  bearer_token          = var.datadog_wif_bootstrap_pat
}
# Avoid provisioning optional forwarder dependencies before WIF exists.
resource "datadog_integration_aws_account" "datadog_integration" {
  logs_config {
    lambda_forwarder {}
  }
}
'''


class BootstrapError(RuntimeError):
    pass


class ApiError(BootstrapError):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"Datadog returned HTTP {status} for {path.split('?')[0]}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_request(site, pat, path, method="GET", body=None):
    if site not in SITES or not path.startswith("/api/"):
        raise BootstrapError("Unsupported Datadog API destination")
    headers = {
        "Authorization": "Bearer " + pat,
        "Content-Type": "application/vnd.api+json",
        "Dd-Aws-Api-Call-Source": "terraform-wif-bootstrap",
    }
    request = urllib.request.Request(
        f"https://api.{site}{path}", headers=headers, method=method,
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise ApiError(error.code, path) from None
    except (OSError, ValueError):
        raise BootstrapError("Unable to read the Datadog API response") from None


def role_pattern(arn, account_id):
    match = re.fullmatch(r"arn:aws:sts::([0-9]{12}):assumed-role/([A-Za-z0-9_+=,.@-]{1,64})/[^/]+", arn)
    if not match or match[1] != account_id:
        raise BootstrapError("Use an assumed AWS role in the account being integrated; IAM-user credentials and other AWS partitions are not supported")
    if match[2] == "DatadogIntegrationRole":
        raise BootstrapError("Use a separate deployment role, not the DatadogIntegrationRole managed by this configuration")
    return f"arn:aws:sts::{account_id}:assumed-role/{match[2]}/*"


def clean_environment(environment):
    if any(key.startswith("TF_CLI_ARGS") and value for key, value in environment.items()):
        raise BootstrapError("Unset TF_CLI_ARGS overrides before bootstrapping so the reviewed Terraform commands are not changed")
    result = dict(environment)
    for key in (
        "DD_API_KEY", "DATADOG_API_KEY", "DD_APP_KEY", "DATADOG_APP_KEY",
        "DD_BEARER_TOKEN", "DATADOG_BEARER_TOKEN", "DD_CLOUD_PROVIDER_TYPE", "DATADOG_CLOUD_PROVIDER_TYPE",
        "DD_ORG_UUID", "DATADOG_ORG_UUID", "DD_BOOTSTRAP_PAT", PAT_VARIABLE,
        "TF_LOG", "TF_LOG_PATH", "TF_LOG_CORE", "TF_LOG_PROVIDER",
    ):
        result.pop(key, None)
    # Both providers must use the same standard AWS credential chain/profile.
    result["AWS_SDK_LOAD_CONFIG"] = "1"
    return result


def run_command(command, directory, environment, capture=False, allow_failure=False):
    result = subprocess.run(command, cwd=directory, env=environment, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode and not allow_failure:
        # Captured output can include provider diagnostics. Do not echo secrets.
        raise BootstrapError(f"{Path(command[0]).name} {command[1]} failed (exit {result.returncode})")
    return result


def _service_account(site, pat, org_uuid, service_account_id):
    user = api_request(site, pat, f"/api/v2/users/{service_account_id}")["data"]
    attrs = user["attributes"]
    if not attrs.get("service_account") or attrs.get("disabled"):
        raise BootstrapError("Select an active Datadog service account, not a human user")
    org = user.get("relationships", {}).get("org", {}).get("data") or {}
    if org.get("id", "").lower() != org_uuid:
        raise BootstrapError("The PAT, service account, and organization UUID must belong to the same organization")
    permissions = api_request(site, pat, f"/api/v2/users/{service_account_id}/permissions").get("data", [])
    names = {p.get("attributes", {}).get("name") for p in permissions}
    if not REQUIRED_PERMISSIONS.issubset(names):
        raise BootstrapError("The service account needs AWS Configuration Read/Edit and AWS Configurations Manage permissions")
    return attrs["handle"]


def _find_mapping(site, pat, pattern, service_account_id):
    mappings = api_request(site, pat, MAPPING_PATH).get("data", [])
    matches = [m for m in mappings if m["attributes"].get("arn_pattern") == pattern]
    if not matches:
        return None
    if len(matches) != 1 or matches[0]["attributes"].get("account_uuid") != service_account_id:
        raise BootstrapError("A conflicting WIF mapping exists; no mapping was changed")
    return matches[0]["id"]


def _outputs(terraform, directory, environment):
    result = run_command([terraform, "output", "-json", "-no-color", OUTPUT], directory, environment, capture=True, allow_failure=True)
    if result.returncode:
        if f'Output "{OUTPUT}" not found' in (result.stderr or ""):
            return None
        raise BootstrapError("Unable to read Terraform bootstrap state; no fresh registration was attempted")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise BootstrapError("Invalid Terraform bootstrap output") from None


def _verify_outputs(outputs, args, pattern):
    expected = {"contract_version": 1, "aws_account_id": args.account_id, "org_uuid": args.org_uuid, "site": args.site, "arn_pattern": pattern}
    if not isinstance(outputs, dict) or any(outputs.get(key) != value for key, value in expected.items()):
        raise BootstrapError("Terraform output does not match the selected org/account or the AWS CLI role. Use the generated configuration and the same AWS credentials for both providers")
    if not outputs.get("integration_id"):
        raise BootstrapError("Terraform did not return an integration ID")


def bootstrap(args, pat):
    directory = Path(args.directory).resolve()
    environment = clean_environment(os.environ)
    if args.aws_profile:
        environment["AWS_PROFILE"] = args.aws_profile
    version = run_command([args.terraform, "version", "-json"], directory, environment, capture=True)
    try:
        number = json.loads(version.stdout)["terraform_version"]
        match = re.match(r"^(\d+)\.(\d+)", number)
        if not match or (int(match[1]), int(match[2])) < (1, 10):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise BootstrapError("Terraform 1.10 or newer is required for ephemeral credentials") from None
    overrides = [p for p in directory.iterdir() if p.name in {"override.tf", "override.tf.json"} or p.name.endswith(("_override.tf", "_override.tf.json"))]
    if overrides:
        raise BootstrapError("Remove existing Terraform override files before bootstrapping; they will not be overwritten")
    identity = run_command([args.aws, "sts", "get-caller-identity", "--output", "json"], directory, environment, capture=True)
    pattern = role_pattern(json.loads(identity.stdout)["Arn"], args.account_id)
    handle = _service_account(args.site, pat, args.org_uuid, args.service_account_id)
    mapping_id = _find_mapping(args.site, pat, pattern, args.service_account_id)

    run_command([args.terraform, "init", "-input=false"], directory, environment)
    outputs = _outputs(args.terraform, directory, environment)
    accounts = api_request(args.site, pat, f"/api/v2/integration/aws/accounts?aws_account_id={args.account_id}").get("data", [])
    accounts = [a for a in accounts if a.get("attributes", {}).get("aws_account_id") == args.account_id]
    if outputs:
        _verify_outputs(outputs, args, pattern)
        if len(accounts) != 1 or accounts[0]["id"] != outputs["integration_id"]:
            raise BootstrapError("The integration is missing or no longer matches this Terraform state; refusing to adopt or modify another integration")
    elif accounts:
        raise BootstrapError("This AWS account is already integrated outside this generated Terraform state. Automatic migration/import is not supported")
    else:
        state = run_command([args.terraform, "state", "list"], directory, environment, capture=True, allow_failure=True)
        if state.returncode and "No state file was found" not in (state.stderr or ""):
            raise BootstrapError("Unable to read Terraform state; no fresh registration was attempted")
        if RESOURCE in (state.stdout or "").splitlines():
            raise BootstrapError("An existing integration resource lacks the bootstrap contract output; use an explicit migration rather than changing its authentication implicitly")

    approve = ["-auto-approve"] if args.auto_approve else []
    if not outputs and not mapping_id:
        override = directory / OVERRIDE_NAME
        descriptor = os.open(override, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            file.write(OVERRIDE)
        try:
            initial_environment = {**environment, PAT_VARIABLE: pat}
            print("Bootstrap phase: review registration of the Datadog AWS integration. Optional AWS components are deferred until WIF is ready.")
            run_command([args.terraform, "apply", "-target=" + RESOURCE, *approve], directory, initial_environment)
            outputs = _outputs(args.terraform, directory, environment)
            _verify_outputs(outputs, args, pattern)
        finally:
            if override.exists():
                if override.read_text() != OVERRIDE:
                    raise BootstrapError("The temporary override was modified; it was preserved. Remove it explicitly before using WIF")
                override.unlink()

    if not mapping_id:
        # The WIF config service may briefly lag the integration registration.
        for attempt in range(8):
            try:
                mapping_id = _find_mapping(args.site, pat, pattern, args.service_account_id)
                if mapping_id:
                    break
                mapping = api_request(args.site, pat, MAPPING_PATH, method="POST", body={"data": {
                    "type": "aws_cloud_auth_config",
                    "attributes": {"arn_pattern": pattern, "account_identifier": handle},
                }})["data"]
                mapping_id = mapping["id"]
                break
            except ApiError as error:
                if error.status not in {400, 409} or attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 8))
    # No temporary provider override or PAT is present from this point onward.
    print("WIF mapping is ready. Verifying the published Terraform provider using AWS identity, not the PAT.")
    for attempt in range(8):
        result = run_command([
            args.terraform, "plan", "-input=false", "-refresh=false",
            "-target=data.datadog_integration_aws_iam_permissions.datadog_permissions",
        ], directory, environment, capture=True, allow_failure=True)
        if result.returncode == 0:
            break
        if attempt == 7:
            raise BootstrapError("WIF provider verification failed. The mapping is retained; check the service account and AWS role, then retry normal terraform plan/apply. Do not leave a bootstrap override in place")
        time.sleep(min(2 ** attempt, 8))
    print("WIF phase: review and apply the complete configuration.")
    run_command([args.terraform, "apply", *approve], directory, environment)
    print("Complete. Future terraform plan/apply/destroy use AWS WIF without the PAT. The mapping is retained until explicitly revoked; destroy managed resources before revoking it.")
    return mapping_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", required=True, choices=sorted(SITES))
    parser.add_argument("--org-uuid", required=True)
    parser.add_argument("--service-account-id", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--directory", default=".")
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--aws", default="aws")
    parser.add_argument("--aws-profile")
    parser.add_argument("--auto-approve", action="store_true", help="Explicitly approve both Terraform phases (intended for reviewed CI jobs)")
    args = parser.parse_args()
    try:
        args.org_uuid = str(uuid.UUID(args.org_uuid))
        args.service_account_id = str(uuid.UUID(args.service_account_id))
        if not re.fullmatch(r"[0-9]{12}", args.account_id):
            raise ValueError()
    except ValueError:
        parser.error("Use valid organization/service-account UUIDs and a 12-digit AWS account ID")
    pat = os.environ.pop("DD_BOOTSTRAP_PAT", None)
    if not pat:
        if not sys.stdin.isatty():
            parser.error("Provide DD_BOOTSTRAP_PAT through a CI secret, or run interactively to paste it without echo")
        pat = getpass.getpass("Bootstrap PAT (not saved): ")
    if not pat:
        parser.error("A bootstrap PAT is required")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    try:
        bootstrap(args, pat)
    except (BootstrapError, OSError, KeyError, ValueError) as error:
        # API errors are deliberately sanitized; never dump payloads/headers.
        message = str(error) if isinstance(error, BootstrapError) else "Bootstrap failed; verify the generated configuration and prerequisites"
        print(message, file=sys.stderr)
        return 1
    finally:
        pat = None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
