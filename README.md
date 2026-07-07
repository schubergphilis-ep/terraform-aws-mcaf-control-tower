# terraform-aws-mcaf-control-tower

> [!IMPORTANT]
> This module requires **AWS Control Tower v4.0 or higher**.

Terraform module that sets up the core of an AWS Control Tower landing zone:

- 2 **core accounts** (audit + logging) in AWS Organizations.
- 3 **IAM service roles** in the management account (`AWSControlTowerAdmin`, `AWSControlTowerCloudTrailRole`, `AWSControlTowerStackSetRole`).
- A **KMS key** for centralized log encryption, or use an existing one via `kms_key.existing_arn`.
- The **`aws_controltower_landing_zone`** resource.

For additional features (Security Hub, GuardDuty, Config, SSO permission sets, etc.) use the [`schubergphilis/mcaf-landing-zone/aws`](https://github.com/schubergphilis/terraform-aws-mcaf-landing-zone) module.

To migrate an existing console-managed Control Tower setup into this module, see [MIGRATION.md](./MIGRATION.md).

## Prerequisites

An **AWS Organization must already exist** in the management account or be deployed together with this module. Create (or import) it with the [`schubergphilis/mcaf-organization/aws`](https://registry.terraform.io/modules/schubergphilis/mcaf-organization/aws/latest) module. Control Tower cannot create the organization, and the audit/logging accounts this module creates must live inside it.

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
|------|---------|
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.9.0 |
| <a name="requirement_aws"></a> [aws](#requirement\_aws) | >= 6.40 |

## Providers

| Name | Version |
|------|---------|
| <a name="provider_aws"></a> [aws](#provider\_aws) | >= 6.40 |

## Modules

| Name | Source | Version |
|------|--------|---------|
| <a name="module_control_tower_admin"></a> [control\_tower\_admin](#module\_control\_tower\_admin) | schubergphilis-ep/mcaf-role/aws | ~> 0.5.3 |
| <a name="module_control_tower_cloudtrail"></a> [control\_tower\_cloudtrail](#module\_control\_tower\_cloudtrail) | schubergphilis-ep/mcaf-role/aws | ~> 0.5.3 |
| <a name="module_control_tower_kms"></a> [control\_tower\_kms](#module\_control\_tower\_kms) | schubergphilis-ep/mcaf-kms/aws | ~> 1.0.0 |
| <a name="module_control_tower_stackset"></a> [control\_tower\_stackset](#module\_control\_tower\_stackset) | schubergphilis-ep/mcaf-role/aws | ~> 0.5.3 |

## Resources

| Name | Type |
|------|------|
| [aws_controltower_landing_zone.default](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/controltower_landing_zone) | resource |
| [aws_iam_policy.control_tower_admin](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_policy) | resource |
| [aws_iam_policy.control_tower_stackset](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_policy) | resource |
| [aws_iam_role_policy_attachment.control_tower_admin](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_iam_role_policy_attachment.control_tower_stackset](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/iam_role_policy_attachment) | resource |
| [aws_organizations_account.audit](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/organizations_account) | resource |
| [aws_organizations_account.logging](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/organizations_account) | resource |
| [aws_organizations_organizational_unit.default](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/organizations_organizational_unit) | resource |
| [aws_caller_identity.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/caller_identity) | data source |
| [aws_iam_policy_document.admin](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.control_tower_kms](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_policy_document.stackset](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_policy_document) | data source |
| [aws_iam_session_context.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/iam_session_context) | data source |
| [aws_organizations_organization.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/organizations_organization) | data source |
| [aws_region.current](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/data-sources/region) | data source |

## Inputs

| Name | Description | Type | Default | Required |
|------|-------------|------|---------|:--------:|
| <a name="input_audit_account"></a> [audit\_account](#input\_audit\_account) | Core audit account that Control Tower uses for security roles. The email must be unique across AWS. | <pre>object({<br/>    name  = optional(string, "core-audit")<br/>    email = string<br/>  })</pre> | n/a | yes |
| <a name="input_logging_account"></a> [logging\_account](#input\_logging\_account) | Core logging account that Control Tower uses as the centralized log archive. The email must be unique across AWS. | <pre>object({<br/>    name  = optional(string, "core-logging")<br/>    email = string<br/>  })</pre> | n/a | yes |
| <a name="input_access_logging_bucket_retention_days"></a> [access\_logging\_bucket\_retention\_days](#input\_access\_logging\_bucket\_retention\_days) | Retention (in days) applied to the centralized access logging bucket. | `number` | `1095` | no |
| <a name="input_additional_governed_regions"></a> [additional\_governed\_regions](#input\_additional\_governed\_regions) | Additional regions governed by AWS Control Tower. The current region & us-east-1 are always governed. | `list(string)` | `[]` | no |
| <a name="input_config_access_logging_retention_days"></a> [config\_access\_logging\_retention\_days](#input\_config\_access\_logging\_retention\_days) | Retention (in days) applied to the AWS Config access logging bucket. | `number` | `1095` | no |
| <a name="input_config_logging_retention_days"></a> [config\_logging\_retention\_days](#input\_config\_logging\_retention\_days) | Retention (in days) applied to the AWS Config logging bucket. | `number` | `365` | no |
| <a name="input_control_tower_version"></a> [control\_tower\_version](#input\_control\_tower\_version) | AWS Control Tower landing zone version. Must be 4.0 or higher — this module renders the v4.0 manifest schema (top-level `config` section, `securityRoles.enabled`, no `organizationStructure`), which is incompatible with v3.x. To migrate an existing v3.x landing zone, bump it to 4.0 via the AWS console first; Terraform will then plan a clean diff. | `string` | `"4.0"` | no |
| <a name="input_kms_key"></a> [kms\_key](#input\_kms\_key) | KMS configuration for Control Tower log encryption. | <pre>object({<br/>    existing_arn = optional(string)<br/><br/>    iam_arns_administrative = optional(list(string), [])<br/>    iam_arns_decrypt        = optional(list(string), [])<br/>  })</pre> | `{}` | no |
| <a name="input_landing_zone_timeouts"></a> [landing\_zone\_timeouts](#input\_landing\_zone\_timeouts) | Per-operation timeouts for the `aws_controltower_landing_zone` resource. | <pre>object({<br/>    create = optional(string, "180m")<br/>    update = optional(string, "180m")<br/>    delete = optional(string, "180m")<br/>  })</pre> | `{}` | no |
| <a name="input_logging_bucket_retention_days"></a> [logging\_bucket\_retention\_days](#input\_logging\_bucket\_retention\_days) | Retention (in days) applied to the centralized logging bucket. | `number` | `365` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags to apply to all resources created by this module. | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
|------|-------------|
| <a name="output_audit_account_arn"></a> [audit\_account\_arn](#output\_audit\_account\_arn) | ARN of the core audit account. |
| <a name="output_audit_account_id"></a> [audit\_account\_id](#output\_audit\_account\_id) | ID of the core audit account. |
| <a name="output_core_account_ids"></a> [core\_account\_ids](#output\_core\_account\_ids) | Map of core account IDs (audit, management, logging). |
| <a name="output_governed_regions"></a> [governed\_regions](#output\_governed\_regions) | List of governed regions configured for Control Tower. |
| <a name="output_kms_key_arn"></a> [kms\_key\_arn](#output\_kms\_key\_arn) | ARN of the KMS key used by Control Tower for log encryption. Equals `var.kms_key.existing_arn` when one is supplied. |
| <a name="output_kms_key_id"></a> [kms\_key\_id](#output\_kms\_key\_id) | ID of the KMS key used by Control Tower for log encryption. Null when `kms_key.existing_arn` is supplied. |
| <a name="output_landing_zone_arn"></a> [landing\_zone\_arn](#output\_landing\_zone\_arn) | ARN of the AWS Control Tower landing zone. |
| <a name="output_landing_zone_id"></a> [landing\_zone\_id](#output\_landing\_zone\_id) | Identifier of the AWS Control Tower landing zone. |
| <a name="output_logging_account_arn"></a> [logging\_account\_arn](#output\_logging\_account\_arn) | ARN of the core logging account. |
| <a name="output_logging_account_id"></a> [logging\_account\_id](#output\_logging\_account\_id) | ID of the core logging account. |
<!-- END_TF_DOCS -->