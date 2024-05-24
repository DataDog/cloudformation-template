#!/bin/bash

aws sso login --profile sso-sandbox-account-admin

TASKCAT_S3_BUCKET="datadog-cloudformation-templates-aws-taskcat-test"
TASKCAT_S3_PREFIX="aws-quickstart"

if [ -z "$DD_API_KEY" ]; then
    echo "Must specify a Datadog API key"
    exit 1
fi

if [ -z "$DD_APP_KEY" ]; then
    echo "Must specify a Datadog APP key"
    exit 1
fi

mkdir -p ./tmp

for f in ../*.yaml; do
   sed "s|<BUCKET_PLACEHOLDER>.s3.amazonaws.com/aws/<VERSION_PLACEHOLDER>|${TASKCAT_S3_BUCKET}.s3.amazonaws.com/${TASKCAT_S3_PREFIX}|g" $f > ./tmp/$(basename $f)
done

sed "s|<REPLACE_DD_API_KEY>|${DD_API_KEY}|g ; s|<REPLACE_DD_APP_KEY>|${DD_APP_KEY}|g" ./.taskcat.yml > ./tmp/.taskcat.yml

taskcat upload -b ${TASKCAT_S3_BUCKET} -k ${TASKCAT_S3_PREFIX} -p tmp

taskcat test run --skip-upload --project-root tmp --no-delete

echo "To delete test stacks, run:"
echo " taskcat test clean aws-quickstart -a sso-sandbox-account-admin -r us-east-2"
