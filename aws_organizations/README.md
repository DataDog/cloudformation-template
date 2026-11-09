# Datadog AWS Integration with Organizations

This guide provides an overview of the process for integrating multiple accounts within your AWS Organization or Organizational Unit with Datadog.

The Cloudformation StackSet template provided by Datadog automates the creation of the required IAM role and associated policy in every AWS account under an Organization or Organizational Unit (OU), eliminating the need for manual setup and configuration.

The Datadog Cloudformation StackSet performs the following steps:

- Deploys the Datadog AWS integration CloudFormation StackSet template in the root account of an AWS Organization or Organization Unit.
- Automatically creates the necessary IAM role and policies in the target accounts.
- Automatically initiates ingestion of AWS CloudWatch metrics and events from the AWS resources in the accounts.
- Optionally configures Datadog Cloud Security Management to monitor resource misconfigurations in your AWS accounts.

## Prerequisites

Before getting started, ensure you have the following prerequisites:
- Access to the management account: Your AWS user needs to be able to access the management account.
- An account administrator has enabled Trusted access with AWS Organizations: Refer to AWS Docs on how to enable trusted access between StackSets and Organizations to create & deploy stacks using service-managed permissions.
  
## Installation

1. Go to the AWS integration configuration page in Datadog and click "Add AWS Account".
2. Select the integration’s settings under the “Add Multiple AWS Accounts” option. You will need these in Step 3.
   1. Use the template specified in this step in the newly created Cloudformation Stackset.
   2. Select your Datadog API key.
   3. Select your Datadog APP key.
3. Click [Launch CloudFormation Template](https://us-east-1.console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacksets/create). This opens the AWS Console and loads the CloudFormation StackSet.
4. Keep the default choice of "Service-managed permissions".
5. Under Specify template, paste the template URL from Step 2 and click Next.
6. Name your StackSet and fill in the API and APP keys you selected in Step 2.
   1. Optionally, enable Cloud Security Posture Management (CSPM) to scan your cloud environment, hosts, and containers for misconfigurations and security risks.
7. In Step 3 of the StackSet template, choose to either deploy the Datadog integration across an Organization or a specific Organizational Unit.
8. Keep Automatic deployment enabled in order to automatically deploy the Datadog AWS Integration in new accounts that are added to the Organization or OU.
9. Select which regions in which you’d like to deploy the integration. Note that you can modify regions to monitor from the Datadog AWS configuration page after deploying the stack.
10. Move to the Review page and Click Submit. This launches the creation process for the Datadog StackSet. This could take a while depending on how many accounts need to be integrated. Ensure that the StackSet successfully creates all resources before proceeding.
11. After the stack is created, go back to the AWS integration tile in Datadog and click Ready!

## Datadog::Integrations::AWS

These templates create the AWS resources and register accounts through Datadog's integration API. The separate [Datadog::Integrations::AWS](https://github.com/DataDog/datadog-cloudformation-resources/tree/master/datadog-integrations-aws-handler) CloudFormation resource is another option for managing integration configuration.

## WIF bootstrap (v4.4.0, opt-in)

`main_organizations_wif.yaml` is a **regular CloudFormation stack**, not a template to paste into the StackSet wizard. It creates a service-managed StackSet using `main_organizations.yaml` with `AuthenticationMode=wif`.

```text
Bootstrap PAT -> Datadog ARN-to-service-account mapping (created once)
Member Lambda -> assume central AWS role -> signed STS proof -> 15-minute Datadog JWT -> integration API
```

The PAT is used only by the bootstrap Lambda. It is a `NoEcho` parameter and is never included in a URL, Lambda environment, output, or member-account StackSet parameter. Member workers contain no Datadog API key, application key, PAT, or SAT. They refresh short-lived JWTs from their AWS identity. Expiring or revoking the bootstrap PAT does not remove the mapping.

### Prerequisites

- Use a commercial AWS partition. GovCloud/China are not supported by this bootstrap flow; existing API-key templates remain available.
- Launch in an **already-integrated** AWS Organizations management account or registered StackSets delegated administrator. Existing AWS WIF APIs require the broker account to be integrated. The bootstrap verifies this rather than silently registering or modifying the management account.
- Enable trusted access between AWS Organizations and CloudFormation StackSets. Select `DELEGATED_ADMIN` when applicable.
- Select an active, dedicated Datadog service account with `aws_configuration_read`, `aws_configuration_edit`, and `aws_configurations_manage`. **WIF delegates all permissions of that account**, not just these three. No SAT is created. Prefer a least-privilege role.
- The browser requires `user_app_keys` and `workload_identity_federation_write` to mint the bootstrap PAT. Running this script also needs user/integration read access and WIF read/write access (`workload_identity_federation_read` / `workload_identity_federation_write`, or their legacy `cloud_auth_config_*` aliases). The PAT must include all permissions granted to the selected service account. The POC frontend does not preflight the returned scopes or cloud prerequisites; the bootstrap verifies them through authenticated API calls and fails without falling back to a key.
- The management account must use the standard IAM-role integration trust: one Datadog account-root principal gated by the exact integration external ID. The bootstrap reads that existing trust and preserves the Datadog principal for member accounts, including on `datad0g.com`; it does not guess a staging account number.

### Deployment

1. Publish both templates from the same release before enabling the frontend flow. The web-ui StackSet WIF flow expects `aws/v4.4.0/main_organizations_wif.yaml`.
2. Visit the StackSet onboarding page with `aws_stackset_setup_wif` enabled. It automatically creates an approximately 24-hour PAT when the caller has the two creation permissions; otherwise Create is disabled with a warning. There is no separate WIF form or browser account/service-account lookup.
3. Launch the **bootstrap stack** in the already-integrated management/delegated-administrator account. The page prefills only `DatadogOrgUUID`, `DatadogSite` and the template URL. Paste the PAT into `BootstrapPAT` in AWS; it is never in the launch URL.
4. Fill in the remaining AWS parameters: `ExpectedAccountId` (the hosting account's 12-digit ID), `ServiceAccountId` (the UUID from the existing Datadog service-account URL), `CallAs`, and the explicit root/OU `OrganizationalUnitIds`. These choices are configured in AWS, not collected by a bespoke web-ui form. The template deploys IAM resources in a single region to avoid cross-region role-name collisions.
5. The bootstrap creates or reuses only an exact matching ARN/identity mapping and verifies a real WIF exchange and authenticated integration read before starting member deployments.
6. New accounts added to the selected targets continue to deploy after the PAT expires. Routine collection-setting updates and deletion use WIF, not the PAT.

This is a new StackSet deployment path, **not an automatic migration** of existing integrations or StackSets. Existing member integrations are rejected without adoption or modification. Failed creation/rollback does not delete integrations the stack does not own.

### Trust and lifecycle

The mapping targets one central assumed-role ARN, `arn:aws:sts::<hosting-account>:assumed-role/<broker-role>/*`. It does not wildcard AWS account IDs. The broker has no AWS data-access policy. Its trust requires the correct organization, one of the explicitly selected root/OU paths, and the unique StackSet worker role name. The bootstrap execution role is separately allowed to verify the mapping. IAM administrators within the authorized targets control the worker identities and are part of this trust boundary.

Identity bindings and organization/OU targets are immutable for this bootstrap stack. Use a replacement bootstrap with a fresh PAT to change them; rejected changes can roll back to the original binding. This avoids revoking workers' access before their old stack instances finish cleanup.

**Delete affected StackSet instances before removing accounts from authorized OUs/the organization, deleting the service account, or revoking the mapping.** Otherwise fresh authentication is intentionally denied and Datadog-side cleanup can fail. Mapping revocation prevents new exchanges; already-issued JWTs can remain usable until their short expiry.

The mapping is deliberately retained on bootstrap deletion and is not managed using an expired PAT. Once stack instances are deleted, explicitly revoke the mapping in **Organization Settings → Workload Identity Federation**. The existing service account is never deleted by these templates.

### Development and release

The WIF Python sources, renderer, and tests live in [`wif/`](wif/). See its [README](wif/README.md) for the source-to-template flow and commands for editing, regenerating, testing, and preparing a release. The deployable YAML templates stay in this directory; customers use the published templates and do not run the renderer.

`release.sh` renders all templates into a temporary directory and substitutes the same bucket/version in both templates. It never mutates source templates. `--private` skips the public versions manifest; bucket policy still determines artifact visibility. Publish the matching template release before enabling `aws_stackset_setup_wif` in web-ui (including Frontend Availability). The browser requests the WIF golden path: enabled uses a bootstrap PAT; disabled uses a scoped application key with the existing template. Keep the WIF flag disabled on unsupported sites, including GovCloud; its existing explicit-creation behavior remains available. Issuance/setup errors do not trigger fallback.

References: [Workload Identity Federation](https://docs.datadoghq.com/account_management/workload_identity_federation/) · [Personal Access Tokens](https://docs.datadoghq.com/account_management/personal-access-tokens/).

