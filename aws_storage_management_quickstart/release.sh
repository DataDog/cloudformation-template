#!/bin/bash

# Usage: ./release.sh [<S3_Bucket>] [--gov] [--private] [--yes]
#
# CI runs the commercial release on merge. GovCloud releases are manual:
#   aws-vault exec sso-govcloud-us1-fed-aws-integrations-s3-operator -- ./release.sh --gov --yes

set -e

# Parse flags and optional bucket argument
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
    S3_HOST="s3.us-gov-west-1.amazonaws.com"
    CONSOLE_URL="https://console.amazonaws-us-gov.com/cloudformation/home?region=us-gov-west-1"
else
    if [ -z "$BUCKET" ]; then
        echo "Must specify a S3 bucket to publish the template"
        exit 1
    fi
    S3_HOST="s3.amazonaws.com"
    CONSOLE_URL="https://console.aws.amazon.com/cloudformation/home"
fi

# Read the version
VERSION=$(head -n 1 version.txt)

# Confirm the bucket for the current release doesn't already exist so we don't overwrite it
set +e
response=$(aws s3api head-object \
    --bucket "${BUCKET}" \
    --key "aws_storage_management_quickstart/${VERSION}/storage-management-all-in-one.yaml" > /dev/null 2>&1)

if [[ ${?} -eq 0 ]]; then
    echo "S3 bucket path ${BUCKET}/aws_storage_management_quickstart/${VERSION} already exists. Please up the version."
    exit 1
fi
set -e

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://${BUCKET}/aws_storage_management_quickstart/${VERSION}/$i"
done

if [ "$AUTO_YES" = false ]; then
    read -p "Continue (y/n)?" CONT
    if [ "$CONT" != "y" ]; then
        echo "Exiting"
        exit 1
    fi
else
    echo "Proceeding with upload (--yes flag provided)"
fi

# GovCloud can't load the nested forwarder template from commercial S3, so point it at the
# GovCloud copy (mirrored there by the aws_quickstart gov release). The original file is
# restored on exit.
if [ "$GOV" = true ]; then
    GOV_FORWARDER_KEY="aws/forwarder/latest.yaml"
    if ! aws s3api head-object --bucket "${BUCKET}" --key "${GOV_FORWARDER_KEY}" > /dev/null 2>&1; then
        echo "Forwarder template missing at s3://${BUCKET}/${GOV_FORWARDER_KEY}. Run the aws_quickstart gov release first."
        exit 1
    fi
    cp storage-management-all-in-one.yaml storage-management-all-in-one.yaml.bak
    trap 'mv storage-management-all-in-one.yaml.bak storage-management-all-in-one.yaml' EXIT
    perl -pi -e "s|datadog-cloudformation-template\.s3\.amazonaws\.com/${GOV_FORWARDER_KEY}|${BUCKET}.${S3_HOST}/${GOV_FORWARDER_KEY}|g" storage-management-all-in-one.yaml
fi

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws_storage_management_quickstart/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws_storage_management_quickstart/${VERSION} --recursive --exclude "*" --include "*.yaml"
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "${CONSOLE_URL}#/stacks/create/review?stackName=datadog-storage-management&templateURL=https://${BUCKET}.${S3_HOST}/aws_storage_management_quickstart/${VERSION}/storage-management-all-in-one.yaml"

echo "Done!"
