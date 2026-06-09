#!/bin/bash

# Usage: ./release.sh <S3_Bucket> [--private] [--yes] [--gov [region]]
#
# --gov [region]  Publish for AWS GovCloud. Rewrites the nested-stack S3
#                 endpoint in streams_main.yaml to the region-specific form
#                 (s3.<region>.amazonaws.com), because the default global
#                 s3.amazonaws.com endpoint only resolves in the commercial
#                 partition and a GovCloud StackSet cannot fetch the child
#                 template from it. Region defaults to us-gov-west-1.
#                 Run with GovCloud credentials and a GovCloud bucket.

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
GOV=false
GOV_REGION="us-gov-west-1"
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
        --gov)
            GOV=true
            # Optional region immediately after --gov (anything not starting with --)
            if [[ -n "$2" && "$2" != --* ]]; then
                GOV_REGION="$2"
                shift
            fi
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: ./release.sh <S3_Bucket> [--private] [--yes] [--gov [region]]"
            exit 1
            ;;
    esac
done

# Confirm to proceed
for i in *.yaml; do
    [ -f "$i" ] || break
    echo "About to upload $i to s3://${BUCKET}/aws/$i"
done

if [ "$GOV" = true ]; then
    echo "GovCloud mode: nested-stack endpoint will target s3.${GOV_REGION}.amazonaws.com"
fi

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
trap 'mv streams_main.yaml.bak streams_main.yaml' EXIT
perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" streams_main.yaml

# GovCloud requires a region-specific S3 endpoint for the nested-stack TemplateURL.
# The global s3.amazonaws.com endpoint is commercial-partition only and a GovCloud
# StackSet cannot read the child template from it.
if [ "$GOV" = true ]; then
    perl -pi -e "s/s3\.amazonaws\.com/s3.${GOV_REGION}.amazonaws.com/g" streams_main.yaml
fi

# Upload (target the GovCloud region explicitly when publishing for gov)
S3_CP_REGION_ARG=""
if [ "$GOV" = true ]; then
    S3_CP_REGION_ARG="--region ${GOV_REGION}"
fi
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml" ${S3_CP_REGION_ARG}
else
    aws s3 cp . s3://${BUCKET}/aws --recursive --exclude "*" --include "*.yaml" ${S3_CP_REGION_ARG}
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
if [ "$GOV" = true ]; then
    echo "https://console.amazonaws-us-gov.com/cloudformation/home?region=${GOV_REGION}#/stacks/create/review?stackName=datadog-aws-streams&templateURL=https://s3.${GOV_REGION}.amazonaws.com/${BUCKET}/aws/streams_main.yaml"
else
    echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-streams&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/streams_main.yaml"
fi

echo "Done!"
