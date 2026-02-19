#!/bin/bash

# Usage: ./release.sh <S3_Bucket>

set -e

VERSIONS_BUCKET="datadog-opensource-asset-versions"
VERSIONS_JSON_PATH=".quickstart/versions.json"

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
    echo "Uploading versions.json to s3://${VERSIONS_BUCKET}/quickstart/versions.json..."

    aws s3 cp "${VERSIONS_JSON_PATH}" "s3://${VERSIONS_BUCKET}/quickstart/versions.json"

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

# Create a temporary directory for processed templates
TEMP_DIR=$(mktemp -d)
trap "rm -rf ${TEMP_DIR}" EXIT

# Copy all YAML files to temp directory
cp *.yaml "${TEMP_DIR}/"
cp datadog_agentless_api_call.py "${TEMP_DIR}/"
cp datadog_integration_api_call.py "${TEMP_DIR}/"

# Change to temp directory for processing
cd "${TEMP_DIR}"

# Update placeholder
for template in main_workflow.yaml main_extended_workflow.yaml main_v2.yaml main_extended.yaml; do
    perl -pi -e "s/<BUCKET_PLACEHOLDER>/${BUCKET}/g" $template
    perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION}/g" $template
done

# Process Agentless Scanning templates
for template in datadog_agentless_delegate_role.yaml datadog_agentless_scanning.yaml datadog_agentless_delegate_role_snapshot.yaml datadog_integration_autoscaling_policy.yaml datadog_integration_sds_policy.yaml datadog_agentless_delegate_role_stackset.yaml; do
    # Note: unlike above, here we remove the 'v' prefix from the version
    perl -pi -e "s/<VERSION_PLACEHOLDER>/${VERSION#v}/g" "$template"

    # Replace ZIPFILE_PLACEHOLDER with the contents of the Python file
    perl -i -pe '
        # Read the Python script from stdin
        BEGIN { $p = do { local $/; <STDIN> } }
        # Find the placeholder and capture its indentation
        /^(\s+)<ZIPFILE_PLACEHOLDER>/ && (
            # Replace with the Python script, preserving the indentation
            $_ = join("\n", map { $1 . $_ } split(/\n/, $p)) . "\n"
        )
    ' "$template" < datadog_agentless_api_call.py
done

# Process Integration API Call template
for template in datadog_integration_api_call_v2.yaml; do
    # Replace ZIPFILE_PLACEHOLDER with the contents of the Python file
    perl -i -pe '
        BEGIN { $p = do { local $/; <STDIN> } }
        /^(\s+)<ZIPFILE_PLACEHOLDER>/ && (
            $_ = join("\n", map { $1 . $_ } split(/\n/, $p)) . "\n"
        )
    ' "$template" < datadog_integration_api_call.py
done

# Upload from temp directory
if [ "$PRIVATE_TEMPLATE" = true ] ; then
    aws s3 cp . s3://${BUCKET}/aws/${VERSION} --recursive --exclude "*" --include "*.yaml"
else
    aws s3 cp . s3://${BUCKET}/aws/${VERSION} --recursive --exclude "*" --include "*.yaml" \
        --grants read=uri=http://acs.amazonaws.com/groups/global/AllUsers
fi
echo "Done uploading the template, and here is the CloudFormation quick launch URL"
echo "https://console.aws.amazon.com/cloudformation/home#/stacks/create/review?stackName=datadog-aws-integration&templateURL=https://${BUCKET}.s3.amazonaws.com/aws/${VERSION}/main_v2.yaml"

# Generate and upload versions.json
echo "Generating and uploading versions.json for the new release..."

generate_versions_json
upload_versions_json

echo "Done generating and uploading versions.json!"
echo "Please verify the uploaded file:"
echo "\thttps://${VERSIONS_BUCKET}.s3.amazonaws.com/quickstart/versions.json"

echo "Done!"
