#!/bin/bash

# Usage: ./release.sh <S3_Bucket> [--private] [--yes]

set -e

TEMPLATE=durable_function_event_forwarder.yaml
PREFIX=aws/lambda-durable-function-event-forwarder

# Parse optional flags and the bucket argument
PRIVATE_TEMPLATE=false
AUTO_YES=false
BUCKET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --private)
            PRIVATE_TEMPLATE=true
            shift
            ;;
        --yes)
            AUTO_YES=true
            shift
            ;;
        --*)
            echo "Unknown option: $1"
            echo "Usage: ./release.sh <S3_Bucket> [--private] [--yes]"
            exit 1
            ;;
        *)
            BUCKET=$1
            shift
            ;;
    esac
done

if [ -z "$BUCKET" ]; then
    echo "Must specify a S3 bucket to publish the template"
    exit 1
fi

# The version is the single source of truth in version.txt.
VERSION=$(head -n 1 version.txt)
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "ERROR: version.txt must contain a semver like v1.2.3 (got '$VERSION')"
    exit 1
fi

VERSIONED_KEY="${PREFIX}/${VERSION}.yaml"
LATEST_KEY="${PREFIX}/latest.yaml"

# Refuse to overwrite an already-published immutable version.
if aws s3api head-object --bucket "$BUCKET" --key "$VERSIONED_KEY" >/dev/null 2>&1; then
    echo "ERROR: s3://${BUCKET}/${VERSIONED_KEY} already exists. Bump version.txt and retry."
    exit 1
fi

echo "Validating ${TEMPLATE}..."
aws cloudformation validate-template --template-body "file://${TEMPLATE}" > /dev/null

echo "About to publish ${VERSION} to s3://${BUCKET}/${PREFIX}/ (${VERSION}.yaml + latest.yaml)"
if [ "$AUTO_YES" = false ]; then
    read -p "Continue (y/n)?" CONT
    if [ "$CONT" != "y" ]; then
        echo "Exiting"
        exit 1
    fi
else
    echo "Proceeding with upload (--yes flag provided)"
fi

# Upload the immutable versioned copy plus the moving latest pointer that the
# public install docs reference.
aws s3 cp "$TEMPLATE" "s3://${BUCKET}/${VERSIONED_KEY}" --content-type text/yaml
aws s3 cp "$TEMPLATE" "s3://${BUCKET}/${LATEST_KEY}" --content-type text/yaml

echo "Done. Published:"
echo "  https://${BUCKET}.s3.amazonaws.com/${VERSIONED_KEY}"
echo "  https://${BUCKET}.s3.amazonaws.com/${LATEST_KEY}"
