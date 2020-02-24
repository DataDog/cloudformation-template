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

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://datadog-cloudformation-template/aws/$i"
done
read -p "Continue (y/n)?" CONT
if [ "$CONT" != "y" ]; then
  echo "Exiting"
  exit 1
fi

aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml" \
    --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/main.yaml"

echo "Done!"
