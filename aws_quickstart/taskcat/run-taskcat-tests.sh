#!/bin/bash

# Usage: ./run-taskcat-tests.sh <test_version>

set -e

# Read the S3 bucket
if [ -z "$1" ]; then
    echo "Must specify a test_version (either 'standard' or 'extended')"
    exit 1
else
    TEST_VERSION=$1
fi

if [ "$TEST_VERSION" != "standard" ]  &&  [ "$TEST_VERSION" != "extended" ]; then
    echo "Invalid test_version - Must specify either 'standard' or 'extended'"
    exit 1
fi

if [ -z "$AWS_SSO_PROFILE_NAME" ]; then
    echo "Missing AWS_SSO_PROFILE_NAME - Must specify an AWS profile name"
    exit 1
fi

aws sso login --profile ${AWS_SSO_PROFILE_NAME}

TASKCAT_S3_BUCKET="datadog-cloudformation-templates-aws-taskcat-test"
TASKCAT_PROJECT="aws-quickstart"

if [ -z "$DD_API_KEY" ]; then
    echo "Missing DD_API_KEY - Must specify a Datadog API key"
    exit 1
fi

if [ -z "$DD_APP_KEY" ]; then
    echo "Missing DD_APP_KEY - Must specify a Datadog APP key"
    exit 1
fi

if ! docker info > /dev/null 2>&1; then
    echo "This script uses docker, and it isn't running - please start docker and try again!"
    exit 1
fi

mkdir -p ./tmp

for f in ../*.yaml; do
   sed "s|<BUCKET_PLACEHOLDER>.s3.amazonaws.com/aws/<VERSION_PLACEHOLDER>|${TASKCAT_S3_BUCKET}.s3.amazonaws.com/${TASKCAT_PROJECT}|g" $f > ./tmp/$(basename $f)
done

if [ "$TEST_VERSION" = "standard" ]; then
    cp ./.taskcat.yml ./tmp/.taskcat-temp.yml
elif [ "$TEST_VERSION" = "extended" ]; then
    cp ./.taskcat_extended.yml ./tmp/.taskcat-temp.yml
else
    echo "Invalid test_version - Must specify either 'standard' or 'extended'"
    exit 1
fi

sed "s|<REPLACE_DD_API_KEY>|${DD_API_KEY}|g ; s|<REPLACE_DD_APP_KEY>|${DD_APP_KEY}|g ; s|<REPLACE_AWS_PROFILE>|${AWS_SSO_PROFILE_NAME}|g" ./tmp/.taskcat-temp.yml > ./tmp/.taskcat.yml

taskcat upload -b ${TASKCAT_S3_BUCKET} -k ${TASKCAT_PROJECT} -p tmp

taskcat test run --skip-upload --project-root tmp --no-delete

echo "To delete test stacks, run:"
echo " taskcat test clean ${TASKCAT_PROJECT} -a ${AWS_SSO_PROFILE_NAME}"
