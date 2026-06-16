#!/bin/bash

# Usage: ./release.sh [<S3_Bucket>] [--gov] [--private] [--yes]

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
    BUCKET="${BUCKET:-datadog-cloudformation-stream-template-us-gov}"
else
    if [ -z "$BUCKET" ]; then
        echo "Must specify a S3 bucket to publish the template"
        exit 1
    fi
fi

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://${BUCKET}/aws/$i"
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

# Update bucket placeholder
# Use datadog-cloudformation-stream-template as the s3 template for production
cp streams_main.yaml streams_main.yaml.bak
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" streams_main.yaml
if [ "$GOV" = true ]; then
    # streams_main.yaml references the nested streams_single_region.yaml via a
    # path-style https://s3.amazonaws.com/<bucket>/... URL. GovCloud CloudFormation
    # cannot reach the commercial S3 endpoint, so rewrite it to the GovCloud region.
    perl -pi -e 's|https://s3\.amazonaws\.com/|https://s3.us-gov-west-1.amazonaws.com/|g' streams_main.yaml
fi
trap 'mv streams_main.yaml.bak streams_main.yaml' EXIT

# Upload
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml"
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
if [ "$GOV" = true ]; then
    echo "https://console.amazonaws-us-gov.com/cloudformation/home?region=us-gov-west-1#/stacks/create/review?stackName=datadog-aws-streams&templateURL=https://${BUCKET}.s3.us-gov-west-1.amazonaws.com/aws/streams_main.yaml"
else
    echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-streams&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/streams_main.yaml"
fi

echo "Done!"
