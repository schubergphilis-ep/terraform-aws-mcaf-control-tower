variable "additional_governed_regions" {
  type        = list(string)
  default     = []
  description = "Additional regions governed by AWS Control Tower. The current region & us-east-1 are always governed."
}

variable "audit_account" {
  type = object({
    name  = optional(string, "core-audit")
    email = string
  })
  description = "Core audit account that Control Tower uses for security roles. The email must be unique across AWS."

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.audit_account.email))
    error_message = "audit_account.email must be a syntactically valid email address."
  }

  validation {
    condition     = length(var.audit_account.name) >= 1 && length(var.audit_account.name) <= 50
    error_message = "audit_account.name must be 1–50 characters (AWS Organizations limit)."
  }
}

variable "logging_account" {
  type = object({
    name  = optional(string, "core-logging")
    email = string
  })
  description = "Core logging account that Control Tower uses as the centralized log archive. The email must be unique across AWS."

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.logging_account.email))
    error_message = "logging_account.email must be a syntactically valid email address."
  }

  validation {
    condition     = length(var.logging_account.name) >= 1 && length(var.logging_account.name) <= 50
    error_message = "logging_account.name must be 1–50 characters (AWS Organizations limit)."
  }

  validation {
    condition     = var.logging_account.email != var.audit_account.email
    error_message = "logging_account.email must differ from audit_account.email; AWS Organizations requires unique email addresses per account."
  }
}

variable "logging_bucket_retention_days" {
  type        = number
  default     = 365
  description = "Retention (in days) applied to the centralized logging bucket."

  validation {
    condition     = var.logging_bucket_retention_days >= 1 && var.logging_bucket_retention_days <= 3653
    error_message = "logging_bucket_retention_days must be between 1 and 3653 (AWS Control Tower limit, ~10 years)."
  }
}

variable "access_logging_bucket_retention_days" {
  type        = number
  default     = 1095
  description = "Retention (in days) applied to the centralized access logging bucket."

  validation {
    condition     = var.access_logging_bucket_retention_days >= 1 && var.access_logging_bucket_retention_days <= 3653
    error_message = "access_logging_bucket_retention_days must be between 1 and 3653 (AWS Control Tower limit, ~10 years)."
  }
}

variable "config_logging_retention_days" {
  type        = number
  default     = 365
  description = "Retention (in days) applied to the AWS Config logging bucket."

  validation {
    condition     = var.config_logging_retention_days >= 1 && var.config_logging_retention_days <= 3653
    error_message = "config_logging_retention_days must be between 1 and 3653 (AWS Control Tower limit, ~10 years)."
  }
}

variable "config_access_logging_retention_days" {
  type        = number
  default     = 1095
  description = "Retention (in days) applied to the AWS Config access logging bucket."

  validation {
    condition     = var.config_access_logging_retention_days >= 1 && var.config_access_logging_retention_days <= 3653
    error_message = "config_access_logging_retention_days must be between 1 and 3653 (AWS Control Tower limit, ~10 years)."
  }
}

variable "control_tower_version" {
  type        = string
  default     = "4.0"
  description = "AWS Control Tower landing zone version. Must be 4.0 or higher — this module renders the v4.0 manifest schema (top-level `config` section, `securityRoles.enabled`, no `organizationStructure`), which is incompatible with v3.x. To migrate an existing v3.x landing zone, bump it to 4.0 via the AWS console first; Terraform will then plan a clean diff."

  validation {
    condition     = can(regex("^\\d+\\.\\d+$", var.control_tower_version))
    error_message = "control_tower_version must be in `MAJOR.MINOR` format (for example, \"4.0\")."
  }

  validation {
    condition     = tonumber(split(".", var.control_tower_version)[0]) >= 4
    error_message = "control_tower_version must be 4.0 or higher. The v4.0 manifest schema this module emits (top-level `config`, `securityRoles.enabled`, no `organizationStructure`) is incompatible with Control Tower landing zone v3.x."
  }
}

variable "kms_key" {
  type = object({
    existing_arn = optional(string)

    iam_arns_administrative = optional(list(string), [])
    iam_arns_decrypt        = optional(list(string), [])
  })
  default     = {}
  description = "KMS configuration for Control Tower log encryption."

  validation {
    condition = var.kms_key.existing_arn == null || (
      length(var.kms_key.iam_arns_administrative) == 0 &&
      length(var.kms_key.iam_arns_decrypt) == 0
    )
    error_message = "kms_key.existing_arn and the create-time fields (iam_arns_administrative / iam_arns_decrypt) are mutually exclusive. Drop one or the other."
  }
}

variable "landing_zone_timeouts" {
  type = object({
    create = optional(string, "180m")
    update = optional(string, "180m")
    delete = optional(string, "180m")
  })
  default     = {}
  description = "Per-operation timeouts for the `aws_controltower_landing_zone` resource."
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Tags to apply to all resources created by this module."
}
