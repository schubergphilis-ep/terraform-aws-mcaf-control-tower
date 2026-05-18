output "audit_account_id" {
  description = "ID of the core audit account."
  value       = aws_organizations_account.audit.id
}

output "audit_account_arn" {
  description = "ARN of the core audit account."
  value       = aws_organizations_account.audit.arn
}

output "core_account_ids" {
  description = "Map of core account IDs (audit, management, logging)."
  value = {
    audit      = aws_organizations_account.audit.id
    management = data.aws_caller_identity.current.account_id
    logging    = aws_organizations_account.logging.id
  }
}

output "governed_regions" {
  description = "List of governed regions configured for Control Tower."
  value       = local.manifest.governedRegions
}

output "kms_key_arn" {
  description = "ARN of the KMS key used by Control Tower for log encryption. Equals `var.kms_key.existing_arn` when one is supplied."
  value       = local.kms_key_arn
}

output "kms_key_id" {
  description = "ID of the KMS key used by Control Tower for log encryption. Null when `kms_key.existing_arn` is supplied."
  value       = local.create_kms_key ? module.control_tower_kms[0].id : null
}

output "landing_zone_id" {
  description = "Identifier of the AWS Control Tower landing zone."
  value       = aws_controltower_landing_zone.default.id
}

output "landing_zone_arn" {
  description = "ARN of the AWS Control Tower landing zone."
  value       = aws_controltower_landing_zone.default.arn
}

output "logging_account_id" {
  description = "ID of the core logging account."
  value       = aws_organizations_account.logging.id
}

output "logging_account_arn" {
  description = "ARN of the core logging account."
  value       = aws_organizations_account.logging.arn
}
