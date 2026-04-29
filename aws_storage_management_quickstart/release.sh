#!/bin/bash

# Usage: ./release.sh <S3_Bucket> [--private] [--yes]

set -e

# Read the S3 bucket
if [ -z "$1" ]; then
    echo "Must specify a S3 bucket to publish the template"
    exit 1
else
    BUCKET=$1
fi

# Parse optional flags
PRIVATE_TEMPLATE=false
AUTO_YES=false
shift
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
        *)
            echo "Unknown option: $1"
            echo "Usage: ./release.sh <S3_Bucket> [--private] [--yes]"
            exit 1
            ;;
    esac
done

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

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws_storage_management_quickstart/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws_storage_management_quickstart/${VERSION} --recursive --exclude "*" --include "*.yaml"
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-storage-management&templateURL=https://${BUCKET}.s3.amazonaws.com/aws_storage_management_quickstart/${VERSION}/storage-management-all-in-one.yaml"

echo "Done!"
