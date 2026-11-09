"""AWS identity exchange used by the StackSet bootstrap and account workers.

Embedded into the templates by render_wif.py. No PAT or static Datadog key is
needed by this client. Credentials and signed STS proofs must never be logged.
"""

import base64
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.config import Config
from botocore.exceptions import ClientError

WIF_AWS_CONFIG = Config(connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 2, "mode": "standard"})

WIF_SITES = frozenset({
    "datadoghq.com", "datadoghq.eu", "us3.datadoghq.com", "us5.datadoghq.com",
    "ap1.datadoghq.com", "ap2.datadoghq.com", "uk1.datadoghq.com", "datad0g.com",
})
_WIF_ROLE = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@-]{1,64}$")
_WIF_TOKEN_CACHE = {}


class WifError(RuntimeError):
    """An error whose message is safe to return to CloudFormation."""


class DatadogHttpError(WifError):
    def __init__(self, status, path):
        self.status = status
        # Do not include response bodies, request headers, or signed proofs.
        super().__init__(f"Datadog returned HTTP {status} for {path.split('?')[0]}")


class _NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _wif_api_origin(site):
    if site not in WIF_SITES:
        raise WifError("Unsupported Datadog site for AWS StackSet WIF")
    return f"https://api.{site}"


def wif_api_request(site, path, headers, method="GET", body=None):
    """Call only a supported Datadog API origin, without forwarding credentials."""
    origin = _wif_api_origin(site)
    if not path.startswith("/api/"):
        raise WifError("Invalid Datadog API path")
    request_headers = {
        "Accept": "application/vnd.api+json",
        "Dd-Aws-Api-Call-Source": "cfn-organizations-wif",
        **headers,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/vnd.api+json"
    request = urllib.request.Request(
        origin + path, data=data, headers=request_headers, method=method,
    )
    try:
        with urllib.request.build_opener(_NoCredentialRedirect()).open(request, timeout=15) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise DatadogHttpError(error.code, path) from None
    except (OSError, urllib.error.URLError):
        raise WifError("Unable to reach the Datadog API") from None
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise WifError("Malformed response from the Datadog API") from None


class WifClient:
    def __init__(self, site, org_uuid, role_arn):
        _wif_api_origin(site)
        try:
            if str(uuid.UUID(org_uuid)) != org_uuid.lower():
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise WifError("DatadogOrgUUID must be a canonical organization UUID") from None
        if not isinstance(role_arn, str) or not _WIF_ROLE.fullmatch(role_arn):
            raise WifError("WifBrokerRoleArn must identify one commercial AWS IAM role")
        self.site = site
        self.org_uuid = org_uuid.lower()
        self.role_arn = role_arn

    def _proof(self):
        region = os.environ.get("AWS_REGION", "us-east-1")
        if not re.fullmatch(r"[a-z]{2}-[a-z0-9-]+-[0-9]+", region):
            raise WifError("Invalid AWS region")
        try:
            sts = boto3.client("sts", region_name=region, config=WIF_AWS_CONFIG)
            for attempt in range(5):
                try:
                    result = sts.assume_role(
                        RoleArn=self.role_arn,
                        RoleSessionName="datadog-stackset-wif",
                        DurationSeconds=900,
                    )["Credentials"]
                    break
                except ClientError as error:
                    # Newly created worker policies and broker trust can take a
                    # few seconds to propagate. Never fall back to a PAT/key.
                    if attempt == 4 or error.response.get("Error", {}).get("Code") not in {"AccessDenied", "AccessDeniedException", "NoSuchEntity"}:
                        raise
                    time.sleep(2 ** attempt)
        except Exception:
            raise WifError("Unable to assume the WIF broker role; check its organization, OU, and worker-role trust policy") from None
        credentials = Credentials(result["AccessKeyId"], result["SecretAccessKey"], result["SessionToken"])
        host = f"sts.{region}.amazonaws.com"
        url = f"https://{host}/"
        body = "Action=GetCallerIdentity&Version=2011-06-15"
        request = AWSRequest(
            method="POST", url=url, data=body,
            headers={
                "Host": host,
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                # This header MUST be present before signing: it binds the proof
                # to the Datadog org and prevents cross-org proof replay.
                "X-Ddog-Org-Id": self.org_uuid,
            },
        )
        SigV4Auth(credentials, "sts", region).add_auth(request)
        headers = json.dumps({key: [value] for key, value in request.headers.items()})
        encode = lambda value: base64.b64encode(value.encode("utf-8")).decode("ascii")
        return "|".join((encode(body), encode(headers), "POST", encode(url)))

    def invalidate(self):
        _WIF_TOKEN_CACHE.pop((self.site, self.org_uuid, self.role_arn), None)

    def headers(self):
        cache_key = (self.site, self.org_uuid, self.role_arn)
        cached = _WIF_TOKEN_CACHE.get(cache_key)
        if cached and cached[1] > time.time() + 60:
            return {"Authorization": f"Bearer {cached[0]}"}
        response = wif_api_request(
            self.site, "/api/v2/delegated-token",
            {"Authorization": f"Delegated {self._proof()}"}, method="POST",
        )
        try:
            attributes = response["data"]["attributes"]
            token = attributes["access_token"]
            expiry = float(attributes["expires"])
            if not isinstance(token, str) or not token or not math.isfinite(expiry) or expiry <= time.time() + 60:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise WifError("Datadog returned an invalid or expired WIF token") from None
        # The exchange currently issues 15-minute tokens. Never cache longer
        # than 14 minutes, even if a server response advertises a longer expiry.
        _WIF_TOKEN_CACHE[cache_key] = (token, min(expiry, time.time() + 14 * 60))
        return {"Authorization": f"Bearer {token}"}


def datadog_stackset_urlopen(request, properties):
    if properties.get("AuthenticationMode") == "wif":
        return urllib.request.build_opener(_NoCredentialRedirect()).open(request, timeout=15)
    return urllib.request.urlopen(request)


def datadog_stackset_headers(properties):
    mode = properties.get("AuthenticationMode", "api_keys")
    if mode == "wif":
        return WifClient(
            properties["ApiURL"], properties["DatadogOrgUUID"], properties["WifBrokerRoleArn"],
        ).headers()
    if mode == "api_keys":
        return {"DD-API-KEY": properties["APIKey"], "DD-APPLICATION-KEY": properties["APPKey"]}
    raise WifError("Unknown StackSet authentication mode")
