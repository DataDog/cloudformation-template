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
    --key "aws/${VERSION}/main.yaml" > /dev/null 2>&1)

if [[ ${?} -eq 0 ]]; then
    echo "S3 bucket path ${BUCKET}/aws/${VERSION} already exists. Please up the version."
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
    echo "About to upload $i to s3://${BUCKET}/aws/${VERSION}/$i"
done
read -p "Continue (y/n)?" CONT
if [ "$CONT" != "y" ]; then
  echo "Exiting"
  exit 1
fi

# Update bucket placeholder
cp main_v2.yaml main_v2.yaml.bak
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" main_v2.yaml
perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION}/g" main_v2.yaml

cp main_extended.yaml main_extended.yaml.bak
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" main_extended.yaml
perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION}/g" main_extended.yaml

trap 'mv main_v2.yaml.bak main_v2.yaml;
      mv main_extended.yaml.bak main_extended.yaml;
' EXIT

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws/${VERSION} --recursive --exclude "*" --include "*.yaml" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/${VERSION}/main_v2.yaml"

echo "Done!"
