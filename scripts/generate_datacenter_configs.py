#!/usr/bin/env python3
"""
Generate CloudFormation datacenter configuration from centralized config.

This script reads datacenters.yaml and generates CloudFormation snippets
for use in templates. Generated files are written to the generated/ directory.

Usage:
  python scripts/generate_datacenter_configs.py

Outputs:
  generated/allowed_values_sites.yaml - List for AllowedValues parameter
  generated/mappings_account_ids.yaml - CloudFormation Mappings for account IDs
  generated/mappings_endpoints_logs_v1.yaml - Logs v1 endpoint mappings
  generated/mappings_endpoints_logs_v2.yaml - Logs v2 endpoint mappings
  generated/mappings_endpoints_metrics.yaml - Metrics endpoint mappings
  generated/mappings_endpoints_cloudplatform.yaml - Config stream mappings
"""

import os
import sys
import yaml
from pathlib import Path


def load_config(config_path='datacenters.yaml'):
    """Load datacenters.yaml configuration file."""
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found", file=sys.stderr)
        sys.exit(1)

    with open(config_path, 'r') as f:
        return yaml.safe_load(f)


def generate_allowed_values(config):
    """Generate AllowedValues list for DatadogSite parameter."""
    sites = [dc['site'] for dc in config['datacenters']]

    # Add test datacenters if present
    if 'test_datacenters' in config:
        sites.extend([dc['site'] for dc in config['test_datacenters']])

    return sites


def generate_account_id_mappings(config):
    """Generate CloudFormation Mappings for account IDs."""
    mappings = {'DdAccountIdBySite': {}}

    for dc in config['datacenters']:
        entry = {'AccountId': dc['account_id']}

        # Add GovCloud account ID if present
        if 'account_id_govcloud' in dc:
            entry['AccountIdGovCloud'] = dc['account_id_govcloud']

        mappings['DdAccountIdBySite'][dc['site']] = entry

    # Add test datacenters
    if 'test_datacenters' in config:
        for dc in config['test_datacenters']:
            mappings['DdAccountIdBySite'][dc['site']] = {
                'AccountId': dc['account_id']
            }

    return {'Mappings': mappings}


def generate_endpoint_mappings(config, endpoint_type):
    """Generate CloudFormation Mappings for specific endpoint type."""
    # Create mapping name (e.g., logs_v1 -> DdLogsV1EndpointBySite)
    mapping_name_parts = endpoint_type.split('_')
    mapping_name = 'Dd' + ''.join(p.capitalize() for p in mapping_name_parts) + 'EndpointBySite'

    mappings = {mapping_name: {}}

    for dc in config['datacenters']:
        if endpoint_type in dc['endpoints']:
            mappings[mapping_name][dc['site']] = {
                'Endpoint': dc['endpoints'][endpoint_type]
            }

    # Add test datacenters
    if 'test_datacenters' in config:
        for dc in config['test_datacenters']:
            if endpoint_type in dc['endpoints']:
                mappings[mapping_name][dc['site']] = {
                    'Endpoint': dc['endpoints'][endpoint_type]
                }

    return {'Mappings': mappings}


def ensure_generated_dir():
    """Create generated/ directory if it doesn't exist."""
    generated_dir = Path('generated')
    generated_dir.mkdir(exist_ok=True)
    return generated_dir


def write_yaml_file(filepath, data):
    """Write data to YAML file."""
    with open(filepath, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def main():
    """Main entry point."""
    print("Loading datacenter configuration...")
    config = load_config()

    print(f"Found {len(config['datacenters'])} production datacenters")
    if 'test_datacenters' in config:
        print(f"Found {len(config['test_datacenters'])} test datacenters")

    # Ensure output directory exists
    generated_dir = ensure_generated_dir()

    # Generate AllowedValues list
    print("\nGenerating AllowedValues list...")
    allowed_values = generate_allowed_values(config)
    write_yaml_file(generated_dir / 'allowed_values_sites.yaml', allowed_values)
    print(f"  ✓ Written to generated/allowed_values_sites.yaml ({len(allowed_values)} sites)")

    # Generate account ID mappings
    print("\nGenerating account ID mappings...")
    account_mappings = generate_account_id_mappings(config)
    write_yaml_file(generated_dir / 'mappings_account_ids.yaml', account_mappings)
    print(f"  ✓ Written to generated/mappings_account_ids.yaml")

    # Generate endpoint mappings for each type
    endpoint_types = ['logs_v1', 'logs_v2', 'metrics', 'cloudplatform', 'api']

    for endpoint_type in endpoint_types:
        print(f"\nGenerating {endpoint_type} endpoint mappings...")
        endpoint_mappings = generate_endpoint_mappings(config, endpoint_type)
        filename = f'mappings_endpoints_{endpoint_type}.yaml'
        write_yaml_file(generated_dir / filename, endpoint_mappings)

        # Count how many sites have this endpoint type
        count = sum(1 for dc in config['datacenters'] if endpoint_type in dc['endpoints'])
        print(f"  ✓ Written to generated/{filename} ({count} endpoints)")

    print("\n✅ All datacenter configuration files generated successfully!")
    print(f"\nGenerated files are in the 'generated/' directory.")
    print("These files can be used to update CloudFormation templates.")


if __name__ == '__main__':
    main()
