#!/usr/bin/env python3
"""
Validate datacenters.yaml configuration file.

This script validates the structure and content of datacenters.yaml to ensure:
- Required fields are present
- Account IDs are valid format
- Endpoint URLs are well-formed
- No duplicate sites

Usage:
  python scripts/validate_datacenters_config.py

Exit codes:
  0 - Validation passed
  1 - Validation failed
"""

import os
import sys
import yaml
import re
from urllib.parse import urlparse


def load_config(config_path='datacenters.yaml'):
    """Load and parse datacenters.yaml."""
    if not os.path.exists(config_path):
        print(f"❌ Error: {config_path} not found", file=sys.stderr)
        return None

    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"❌ Error: Invalid YAML syntax: {e}", file=sys.stderr)
        return None


def validate_account_id(account_id, site):
    """Validate AWS account ID format (12 digits)."""
    if not isinstance(account_id, str):
        print(f"❌ Error: Account ID for {site} must be a string (got {type(account_id).__name__})")
        return False

    if not re.match(r'^\d{12}$', account_id):
        print(f"❌ Error: Invalid account ID for {site}: '{account_id}' (must be 12 digits)")
        return False

    return True


def validate_endpoint_url(url, site, endpoint_type):
    """Validate endpoint URL format."""
    if not isinstance(url, str):
        print(f"❌ Error: Endpoint {endpoint_type} for {site} must be a string")
        return False

    if not url.startswith('https://'):
        print(f"❌ Error: Endpoint {endpoint_type} for {site} must use HTTPS: {url}")
        return False

    try:
        parsed = urlparse(url)
        if not parsed.netloc:
            print(f"❌ Error: Invalid URL for {endpoint_type} endpoint on {site}: {url}")
            return False
    except Exception as e:
        print(f"❌ Error: Failed to parse URL for {endpoint_type} on {site}: {e}")
        return False

    return True


def validate_datacenter(dc, seen_sites):
    """Validate a single datacenter entry."""
    errors = []

    # Required fields
    if 'site' not in dc:
        errors.append("Missing required field: 'site'")
        return errors

    site = dc['site']

    # Check for duplicate sites
    if site in seen_sites:
        errors.append(f"Duplicate site: {site}")
    seen_sites.add(site)

    # Validate required fields
    if 'region' not in dc:
        errors.append(f"Missing 'region' for {site}")

    if 'account_id' not in dc:
        errors.append(f"Missing 'account_id' for {site}")
    else:
        if not validate_account_id(dc['account_id'], site):
            errors.append(f"Invalid account_id for {site}")

    # Validate optional GovCloud account ID
    if 'account_id_govcloud' in dc:
        if not validate_account_id(dc['account_id_govcloud'], f"{site} (GovCloud)"):
            errors.append(f"Invalid account_id_govcloud for {site}")

    # Validate endpoints
    if 'endpoints' not in dc:
        errors.append(f"Missing 'endpoints' for {site}")
    else:
        endpoints = dc['endpoints']
        if not isinstance(endpoints, dict):
            errors.append(f"'endpoints' for {site} must be a dictionary")
        else:
            # At least one endpoint is required
            if len(endpoints) == 0:
                errors.append(f"No endpoints defined for {site}")

            # Validate each endpoint URL
            for endpoint_type, url in endpoints.items():
                if not validate_endpoint_url(url, site, endpoint_type):
                    errors.append(f"Invalid {endpoint_type} endpoint for {site}")

    return errors


def main():
    """Main validation logic."""
    print("Validating datacenters.yaml...\n")

    config = load_config()
    if config is None:
        return 1

    errors = []
    seen_sites = set()

    # Validate top-level structure
    if 'version' not in config:
        errors.append("Missing 'version' field")

    if 'datacenters' not in config:
        errors.append("Missing 'datacenters' field")
        print(f"❌ Validation failed with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    if not isinstance(config['datacenters'], list):
        errors.append("'datacenters' must be a list")
        print(f"❌ Validation failed with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1

    if len(config['datacenters']) == 0:
        errors.append("No datacenters defined")

    # Validate each datacenter
    print(f"Validating {len(config['datacenters'])} production datacenters...")
    for i, dc in enumerate(config['datacenters'], 1):
        dc_errors = validate_datacenter(dc, seen_sites)
        if dc_errors:
            errors.extend([f"Datacenter #{i}: {e}" for e in dc_errors])
        else:
            site = dc.get('site', f'#{i}')
            print(f"  ✓ {site}")

    # Validate test datacenters if present
    if 'test_datacenters' in config:
        print(f"\nValidating {len(config['test_datacenters'])} test datacenters...")
        for i, dc in enumerate(config['test_datacenters'], 1):
            dc_errors = validate_datacenter(dc, seen_sites)
            if dc_errors:
                errors.extend([f"Test datacenter #{i}: {e}" for e in dc_errors])
            else:
                site = dc.get('site', f'#{i}')
                print(f"  ✓ {site}")

    # Print results
    print()
    if errors:
        print(f"❌ Validation failed with {len(errors)} error(s):")
        for error in errors:
            print(f"  - {error}")
        return 1
    else:
        print("✅ Validation passed! All datacenter configurations are valid.")
        return 0


if __name__ == '__main__':
    sys.exit(main())
