# WIF Lambda sources and template rendering

This directory contains the **real Lambda source code** for the AWS Organizations WIF flow, plus its renderer and offline tests. It is not a separate deployed service or a test-only implementation.

AWS runs the Python embedded in the parent directory's CloudFormation YAML templates. The files here let maintainers edit and test that code as normal Python instead of maintaining duplicate copies inside YAML strings. **Customers deploy the published YAML; they do not run the renderer.**

## Files and source of truth

| File | Purpose |
| --- | --- |
| `wif_auth.py` | Shared authentication: assume the broker role, sign an AWS identity proof, exchange it for a Datadog JWT, and cache it until refresh is needed. |
| `wif_bootstrap.py` | Initial PAT-authorized prerequisite checks, WIF mapping creation/reuse, and verification of the WIF exchange. |
| `render_wif.py` | Copy these sources into the marked YAML code blocks, check synchronization, or prepare release templates. |
| `wif_test.py` | Offline authentication, lifecycle, template, and renderer regression tests. |

```text
aws_organizations/
├── main_organizations.yaml       # Member-account template
├── main_organizations_wif.yaml   # Central bootstrap template
├── release.sh
└── wif/
    ├── README.md
    ├── wif_auth.py
    ├── wif_bootstrap.py
    ├── render_wif.py
    └── wif_test.py

wif_auth.py
    └──→ main_organizations.yaml: BEGIN GENERATED WIF AUTH

wif_auth.py + wif_bootstrap.py
    └──→ main_organizations_wif.yaml: BEGIN GENERATED WIF BOOTSTRAP
```

The `.py` files are authoritative for those **generated blocks only**. Edit them here, not their embedded copies. The rest of each YAML file remains hand-maintained.

The renderer preserves YAML indentation and combines the shared authentication and bootstrap code into one inline Lambda module. It removes the bootstrap's local `wif_auth` import because those helpers are now in that same module. CloudFormation packages `Code.ZipFile` as the Lambda code; AWS does not fetch these `.py` files from the repository.

## Edit and regenerate

Run the following commands from `aws_organizations/` (the parent of this directory):

```sh
# After editing wif/wif_auth.py or wif/wif_bootstrap.py:
python wif/render_wif.py

# Read-only check; fails if either embedded copy is stale:
python wif/render_wif.py --check
```

Commit both the Python changes and the regenerated YAML. CI runs `--check` to prevent testing updated Python while deploying an old embedded copy. The renderer locates its source files and templates relative to its own file, not the shell's working directory.

Rendering is a maintainer/build step, **not** something Lambda or CloudFormation runs during deployment.

## Test

Use a Python environment with `boto3`, `PyYAML`, and `cfn-lint` installed. From `aws_organizations/`:

```sh
python -B -m unittest discover -s wif -p '*test.py' -v
cfn-lint main_organizations.yaml main_organizations_wif.yaml

# Existing non-WIF Organizations tests:
python -B -m unittest attach_integration_permissions_test -v
```

The WIF suite exercises real signing code and the deployable inline handlers against fake AWS/Datadog services. It covers operation after PAT expiry, refresh, revocation, conflicting integrations, rollback ownership, and rendering from the new directory layout. It does not deploy cloud resources or create live credentials. Start discovery at `wif/` explicitly, as shown above.

## Prepare a release

The renderer can create a separate release directory without changing checked-in templates:

```sh
python wif/render_wif.py \
  --output /tmp/datadog-wif-release \
  --bucket example-release-bucket \
  --version "$(head -n 1 version.txt)"
```

This command only writes local files. It embeds the current Python and replaces the S3 bucket/version placeholders in the parent directory's YAML files, so the bootstrap references the matching member-template release. Neither the template directory nor this source directory is accepted as the output destination.

The existing [`release.sh`](../release.sh) handles actual publication. It first checks synchronization, renders into a temporary directory, and uploads the YAML artifacts. The `.py` files, tests, and this README are not runtime dependencies and are not uploaded by that release command.

For deployment prerequisites, trust, and cleanup ordering, see the [AWS Organizations guide](../README.md#wif-bootstrap-v440-opt-in).
