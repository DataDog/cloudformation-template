#!/bin/bash

# Usage: ./release.sh [<S3_Bucket>] [--gov] [--private] [--yes]

set -e

VERSIONS_JSON_PATH=".bio_permissions/versions.json"

generate_versions_json() {
    echo "Generating ${VERSIONS_JSON_PATH} for version ${VERSION}..."

    local release_date
    release_date=$(date +%Y-%m-%d)

    mkdir -p "$(dirname "${VERSIONS_JSON_PATH}")"
    rm -f "${VERSIONS_JSON_PATH}"

    jq -r -n \
        --arg ver "${VERSION}" \
        --arg date "${release_date}" \
        '{latest: {version: $ver, release_date: $date}}' \
        > "${VERSIONS_JSON_PATH}"
}

upload_versions_json() {
    aws s3 cp \
        "${VERSIONS_JSON_PATH}" \
        "s3://${VERSIONS_BUCKET}/bio_permissions/versions.json"
}

GOV=false
PRIVATE_TEMPLATE=false
AUTO_YES=false
BUCKET=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --gov)
            GOV=true
            shift
            ;;
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
            echo "Usage: ./release.sh [<S3_Bucket>] [--gov] [--private] [--yes]"
            exit 1
            ;;
        *)
            BUCKET=$1
            shift
            ;;
    esac
done

if [ "$GOV" = true ]; then
    BUCKET="${BUCKET:-datadog-cloudformation-template-us-gov}"
    VERSIONS_BUCKET="datadog-opensource-asset-versions-us-gov"
else
    if [ -z "$BUCKET" ]; then
        echo "Must specify an S3 bucket to publish the template"
        exit 1
    fi
    VERSIONS_BUCKET="datadog-opensource-asset-versions"
fi

VERSION=$(head -n 1 version.txt)
TEMPLATE_KEY="aws_bio_permissions/${VERSION}/main.yaml"

set +e
aws s3api head-object \
    --bucket "${BUCKET}" \
    --key "${TEMPLATE_KEY}" \
    > /dev/null 2>&1
if [[ ${?} -eq 0 ]]; then
    echo "S3 object s3://${BUCKET}/${TEMPLATE_KEY} already exists. Please bump the version."
    exit 1
fi
set -e

echo "About to upload main.yaml to s3://${BUCKET}/${TEMPLATE_KEY}"
if [ "$AUTO_YES" = false ]; then
    read -r -p "Continue (y/n)?" CONT
    if [ "$CONT" != "y" ]; then
        echo "Exiting"
        exit 1
    fi
fi

cp main.yaml main.yaml.bak
perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION}/g" main.yaml
trap 'mv main.yaml.bak main.yaml' EXIT

aws s3 cp main.yaml "s3://${BUCKET}/${TEMPLATE_KEY}"

if [ "$PRIVATE_TEMPLATE" = false ]; then
    generate_versions_json
    upload_versions_json
fi

if [ "$GOV" = true ]; then
    CONSOLE_URL="https://console.amazonaws-us-gov.com/cloudformation/home?region=us-gov-west-1"
    TEMPLATE_URL="https://${BUCKET}.s3.us-gov-west-1.amazonaws.com/${TEMPLATE_KEY}"
else
    CONSOLE_URL="https://console.aws.amazon.com/cloudformation/home"
    TEMPLATE_URL="https://${BUCKET}.s3.amazonaws.com/${TEMPLATE_KEY}"
fi

echo "Done uploading the template. Quick Create URL:"
echo "${CONSOLE_URL}#/stacks/quickcreate?stackName=DatadogBIOPermissions&templateURL=${TEMPLATE_URL}"
