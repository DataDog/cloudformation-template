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

aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml" \
    --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers

echo "Done!"
