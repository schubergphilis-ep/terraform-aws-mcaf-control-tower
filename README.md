# terraform-aws-mcaf-control-tower

> [!IMPORTANT]
> This module requires **AWS Control Tower v4.0 or higher**.
> Set `control_tower_version` to `4.0` or above.

Terraform module that sets up the core of an AWS Control Tower landing zone:

- 2 **core accounts** (audit + logging) in AWS Organizations.
- 3 **IAM service roles** in the management account (`AWSControlTowerAdmin`, `AWSControlTowerCloudTrailRole`, `AWSControlTowerStackSetRole`).
- A **KMS key** for centralized log encryption, or use an existing one via `kms_key.existing_arn`.
- The **`aws_controltower_landing_zone`** resource.

For additional features (Security Hub, GuardDuty, Config, SSO permission sets, etc.) use the [`schubergphilis/mcaf-landing-zone/aws`](https://github.com/schubergphilis/terraform-aws-mcaf-landing-zone) module.

To migrate an existing console-managed Control Tower setup into this module, see [MIGRATION.md](./MIGRATION.md).

## Prerequisites

An **AWS Organization must already exist** in the management account or be deployed together with this module. Create (or import) it with the [`schubergphilis/mcaf-organization/aws`](https://registry.terraform.io/modules/schubergphilis/mcaf-organization/aws/latest) module. Control Tower cannot create the organization, and the audit/logging accounts this module creates must live inside it.
