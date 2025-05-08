#!/bin/bash

# Usage: ./release.sh <S3_Bucket>

set -e

# Read the S3 bucket
if [ -z "$1" ]; then
    echo "Must specify a S3 bucket to publish the template"
    exit 1
else
    BUCKET=$1
fi

# Read the version
VERSION=$(head -n 1 version.txt)

# Confirm the bucket for the current release doesn't already exist so we don't overwrite it
set +e
EXIT_CODE=0
response=$(aws s3api head-object \
    --bucket "${BUCKET}" \
    --key "aws_cloud_cost/${VERSION}/main.yaml" > /dev/null 2>&1)

if [[ ${?} -eq 0 ]]; then
    echo "S3 bucket path ${BUCKET}/aws_cloud_cost/${VERSION} already exists. Please up the version."
    exit 1
fi
set -e

# Confirm that the readme link points to the current release
set +e
response=$(grep -q "aws_cloud_cost/${VERSION}/main.yaml" README.md)
if [[ ${?} -ne 0 ]]; then
    echo "README.md does not point to the current release. Please update the link."
    exit 1
fi
set -e

# Upload templates to a private bucket -- useful for testing
if [[ $# -eq 2 ]] && [[ $2 = "--private" ]]; then
    PRIVATE_TEMPLATE=true
else
    PRIVATE_TEMPLATE=false
fi

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://${BUCKET}/aws_cloud_cost/${VERSION}/$i"
done
read -p "Continue (y/n)?" CONT
if [ "$CONT" != "y" ]; then
  echo "Exiting"
  exit 1
fi

# Update bucket placeholder
cp main.yaml main.yaml.bak
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" main.yaml
perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION}/g" main.yaml

trap 'mv main.yaml.bak main.yaml' EXIT

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws_cloud_cost/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws_cloud_cost/${VERSION} --recursive --exclude "*" --include "*.yaml" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-cloud-cost&templateURL=https://${BUCKET}.s3.amazonaws.com/aws_cloud_cost/${VERSION}/main.yaml"

echo "Done!"
