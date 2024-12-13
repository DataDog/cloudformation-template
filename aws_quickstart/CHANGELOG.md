## 2.0.7 (December 13, 2024)

### Features

* Add additional permissions empowering AWS resource collection and align policy content between templates

## 2.0.6 (December 4, 2024)

### Features

* Add `s3express` permission for Resource Crawler

## 2.0.2 (November 13, 2024)

### Features
* Add GetResourcePolicy permission for secretsmanager

## 1.2.5 (November 5, 2024)

### FEATURES
* Adds additional permission

## 1.2.5 (November 5, 2024)

### FEATURES
* Added DisableResourceCollection field which defaults to False. When False, SecurityAudit policy will be attached to the created Datadog IAM Role, and ExtendedResourceCollection will be enabled in Datadog.
* Added additional permissions to the default set of permissions attached to the created Datadog IAM Role.
