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

# Process Agentless Scanning templates
for template in datadog_agentless_delegate_role.yaml datadog_agentless_scanning.yaml datadog_agentless_delegate_role_snapshot.yaml; do
    # Note: unlike above, here we remove the 'v' prefix from the version
    perl -i.bak -pe "s/<VERSION_PLACEHOLDER>/${VERSION#v}/g" "$template"

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

# Update TemplateURL version in aws_organizations/main_organizations.yaml
ORGANIZATIONS_TEMPLATE="../aws_organizations/main_organizations.yaml"
ORGANIZATIONS_VERSION_UPDATED=false
if [ -f "$ORGANIZATIONS_TEMPLATE" ]; then
    # Check if the version needs updating
    CURRENT_ORG_VERSION=$(grep -oP 'https://datadog-cloudformation-template-quickstart\.s3\.amazonaws\.com/aws/\Kv[0-9]+\.[0-9]+\.[0-9]+(?=/datadog_agentless_scanning\.yaml)' "$ORGANIZATIONS_TEMPLATE" || echo "")
    if [ -n "$CURRENT_ORG_VERSION" ] && [ "$CURRENT_ORG_VERSION" != "$VERSION" ]; then
        ORGANIZATIONS_VERSION_UPDATED=true
        echo "Updating main_organizations.yaml TemplateURL from $CURRENT_ORG_VERSION to $VERSION"
    fi
    cp "$ORGANIZATIONS_TEMPLATE" "$ORGANIZATIONS_TEMPLATE.bak"
    # Update the version in the TemplateURL for datadog_agentless_scanning.yaml
    perl -pi -e "s|(https://datadog-cloudformation-template-quickstart\.s3\.amazonaws\.com/aws/)v[0-9]+\.[0-9]+\.[0-9]+(/datadog_agentless_scanning\.yaml)|\$1${VERSION}\$2|g" "$ORGANIZATIONS_TEMPLATE"
fi

trap 'mv main_v2.yaml.bak main_v2.yaml;
      mv main_extended.yaml.bak main_extended.yaml;
      mv datadog_agentless_scanning.yaml.bak datadog_agentless_scanning.yaml;
      mv datadog_agentless_delegate_role.yaml.bak datadog_agentless_delegate_role.yaml;
      mv datadog_agentless_delegate_role_snapshot.yaml.bak datadog_agentless_delegate_role_snapshot.yaml;
      mv "'$ORGANIZATIONS_TEMPLATE'.bak" "'$ORGANIZATIONS_TEMPLATE'";
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

if [ "$ORGANIZATIONS_VERSION_UPDATED" = true ]; then
    echo ""
    echo "⚠️  WARNING: main_organizations.yaml has been updated with the new version ($VERSION)."
    echo "   You need to release aws_organizations as well by running: cd ../aws_organizations && ./release.sh"
fi

echo "Done!"
