locals {
  manifest = {
    accessManagement = {
      enabled = true
    }
    backup = {
      enabled = false
    }
    centralizedLogging = {
      accountId = aws_organizations_account.logging.id
      enabled   = true
      configurations = {
        accessLoggingBucket = { retentionDays = var.access_logging_bucket_retention_days }
        loggingBucket       = { retentionDays = var.logging_bucket_retention_days }
        kmsKeyArn           = local.kms_key_arn
      }
    }
    config = {
      accountId = aws_organizations_account.audit.id
      enabled   = true
      configurations = {
        accessLoggingBucket = { retentionDays = var.config_access_logging_retention_days }
        loggingBucket       = { retentionDays = var.config_logging_retention_days }
        kmsKeyArn           = local.kms_key_arn
      }
    }
    # Home region is always governed — the AWS Control Tower API requires it. Merge it
    # into the caller-supplied list so callers don't have to remember.
    # `us-east-1` is required regardless of the home region because AWS Control Tower deploys global-service resources (IAM Identity Center, Organizations) there.
    governedRegions = distinct(concat(var.additional_governed_regions, [local.home_region], ["us-east-1"]))
    securityRoles = {
      enabled   = true
      accountId = aws_organizations_account.audit.id
    }
  }
}

# AWS Control Tower landing zone.
# ref: https://docs.aws.amazon.com/controltower/latest/userguide/landing-zone-schemas.html
resource "aws_controltower_landing_zone" "default" {
  version           = var.control_tower_version
  manifest_json     = jsonencode(local.manifest)
  remediation_types = ["INHERITANCE_DRIFT"]
  tags              = var.tags

  timeouts {
    create = var.landing_zone_timeouts.create
    update = var.landing_zone_timeouts.update
    delete = var.landing_zone_timeouts.delete
  }

  depends_on = [
    aws_organizations_account.audit,
    aws_organizations_account.logging,
    module.control_tower_admin,
    module.control_tower_cloudtrail,
    module.control_tower_kms,
    module.control_tower_stackset
  ]
}
