#!/bin/bash

# Usage: ./release.sh <S3_Bucket>

set -e

VERSIONS_BUCKET="datadog-opensource-asset-versions-test2"
VERSIONS_JSON_PATH=".attach_integration_permissions/versions.json"

generate_versions_json() {
    echo "Generating ${VERSIONS_JSON_PATH} for version ${VERSION}..."

    local version_number="${VERSION}"
    local release_date=$(date +%Y-%m-%d)

    # Remove previous versions.json
    mkdir -p "$(dirname "${VERSIONS_JSON_PATH}")"
    rm -f "${VERSIONS_JSON_PATH}"

    local versions_json
    versions_json=$(jq -r -n \
        --arg ver "${version_number}" \
        --arg date "${release_date}" \
        '
        {
            latest: {
                version: $ver,
                release_date: $date
            }
        }
    ')

    echo "${versions_json}" > "${VERSIONS_JSON_PATH}"
    echo "Generated ${VERSIONS_JSON_PATH}"
}

upload_versions_json() {
    echo "Uploading versions.json to s3://${VERSIONS_BUCKET}/attach_integration_permissions/versions.json..."

    aws s3 cp "${VERSIONS_JSON_PATH}" "s3://${VERSIONS_BUCKET}/attach_integration_permissions/versions.json"

    echo "Uploaded versions.json to S3!"
}

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
    --key "aws_attach_integration_permissions/${VERSION}/main.yaml" > /dev/null 2>&1)

if [[ ${?} -eq 0 ]]; then
    echo "S3 bucket path ${BUCKET}/aws_attach_integration_permissions/${VERSION} already exists. Please up the version."
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
    echo "About to upload $i to s3://${BUCKET}/aws_attach_integration_permissions/${VERSION}/$i"
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
    aws s3 cp . s3://${BUCKET}/aws_attach_integration_permissions/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws_attach_integration_permissions/${VERSION} --recursive --exclude "*" --include "*.yaml" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://${BUCKET}.s3.amazonaws.com/aws_attach_integration_permissions/${VERSION}/main.yaml"

# Generate and upload versions.json
echo "Generating and uploading versions.json for the new release..."

generate_versions_json
upload_versions_json

echo "Done generating and uploading versions.json!"
echo "Please verify the uploaded file:"
echo "\thttps://${VERSIONS_BUCKET}.s3.amazonaws.com/attach_integration_permissions/versions.json"

echo "Done!"
