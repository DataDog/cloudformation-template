# How to run taskcat tests

## Setup
```
export DD_API_KEY=<api key for test datadog org>
export DD_APP_KEY=<app key for test datadog org>
export AWS_SSO_PROFILE_NAME=<sso profile name for test aws account>
```

### Run
```
./run-taskcat-test.sh
```

### Cleanup
```
# To delete test stacks, run:
taskcat test clean aws-quickstart -a ${AWS_SSO_PROFILE_NAME}
```
