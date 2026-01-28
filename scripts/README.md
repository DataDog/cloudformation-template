# CloudFormation Template Scripts

This directory contains utility scripts for managing CloudFormation templates.

## Datacenter Configuration Scripts

### `generate_datacenter_configs.py`

Generates CloudFormation configuration snippets from the centralized `datacenters.yaml` file.

**Usage:**
```bash
python scripts/generate_datacenter_configs.py
```

**Outputs:**
- `generated/allowed_values_sites.yaml` - List of all datacenter sites for CloudFormation AllowedValues
- `generated/mappings_account_ids.yaml` - CloudFormation Mappings for AWS account IDs by site
- `generated/mappings_endpoints_logs_v1.yaml` - Logs v1 endpoint mappings
- `generated/mappings_endpoints_logs_v2.yaml` - Logs v2 endpoint mappings
- `generated/mappings_endpoints_metrics.yaml` - Metrics endpoint mappings
- `generated/mappings_endpoints_cloudplatform.yaml` - Config stream endpoint mappings
- `generated/mappings_endpoints_api.yaml` - API endpoint mappings

**When to run:**
- After adding a new datacenter to `datacenters.yaml`
- Before updating CloudFormation templates with new datacenter support
- As part of the release process (future integration)

### `validate_datacenters_config.py`

Validates the structure and content of `datacenters.yaml`.

**Usage:**
```bash
python scripts/validate_datacenters_config.py
```

**Exit codes:**
- `0` - Validation passed
- `1` - Validation failed

**Validation checks:**
- Required fields are present (site, region, account_id, endpoints)
- Account IDs are valid format (12 digits)
- Endpoint URLs are well-formed HTTPS URLs
- No duplicate datacenter sites
- YAML syntax is valid

**When to run:**
- After modifying `datacenters.yaml`
- Before committing changes
- As part of CI/CD validation

## Adding a New Datacenter

1. Edit `datacenters.yaml` and add a new datacenter entry:
   ```yaml
   - site: new-site.datadoghq.com
     region: NEW_REGION
     account_id: "123456789012"
     endpoints:
       api: https://api.new-site.datadoghq.com
       logs_v2: https://aws-kinesis-http-intake.logs.new-site.datadoghq.com/api/v2/logs?dd-protocol=aws-kinesis-firehose
       metrics: https://awsmetrics-intake.new-site.datadoghq.com/api/v2/awsmetrics?dd-protocol=aws-kinesis-firehose
       cloudplatform: https://cloudplatform-intake.new-site.datadoghq.com/api/v2/cloudchanges?dd-protocol=aws-kinesis-firehose
   ```

2. Validate the configuration:
   ```bash
   python scripts/validate_datacenters_config.py
   ```

3. Generate CloudFormation snippets:
   ```bash
   python scripts/generate_datacenter_configs.py
   ```

4. Review generated files in `generated/` directory

5. (Future) Use generated files to update CloudFormation templates

## Requirements

- Python 3.6+
- PyYAML (`pip install pyyaml`)
