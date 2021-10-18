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

# Upload templates to a private bucket -- useful for testing
if [[ $# -eq 2 ]] && [[ $2 = "--private" ]]; then
    PRIVATE_TEMPLATE=true
else
    PRIVATE_TEMPLATE=false
fi

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://${BUCKET}/aws/$i"
done
read -p "Continue (y/n)?" CONT
if [ "$CONT" != "y" ]; then
  echo "Exiting"
  exit 1
fi

# Update bucket placeholder
cp main.yaml main.yaml.bak
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" main.yaml
trap 'mv main.yaml.bak main.yaml' EXIT

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml"
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.zip"
else
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.zip" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers

fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/main.yaml"

echo "Done!"
