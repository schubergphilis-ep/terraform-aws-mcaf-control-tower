locals {
  organization_root_id = data.aws_organizations_organization.current.roots[0].id

  # As map to easily expand functionality to create multiple OUs with directory structure in the future.
  root_level_ous = { security = "Security" }
}

resource "aws_organizations_organizational_unit" "default" {
  for_each = local.root_level_ous

  name      = each.value
  parent_id = local.organization_root_id
  tags      = var.tags
}

# Core audit and logging accounts created up-front so the Control Tower landing zone
# manifest can reference them. Both accounts are hardcoded to the Security OU.
#
# `role_name` is in `ignore_changes` because AWS doesn't return it after creation —
# see https://github.com/hashicorp/terraform-provider-aws/issues/12959.
resource "aws_organizations_account" "audit" {
  name              = var.audit_account.name
  email             = var.audit_account.email
  parent_id         = aws_organizations_organizational_unit.default["security"].id
  role_name         = "AWSControlTowerExecution"
  close_on_deletion = false
  tags              = var.tags

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [role_name]
  }
}

resource "aws_organizations_account" "logging" {
  name              = var.logging_account.name
  email             = var.logging_account.email
  parent_id         = aws_organizations_organizational_unit.default["security"].id
  role_name         = "AWSControlTowerExecution"
  close_on_deletion = false
  tags              = var.tags

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [role_name]
  }
}
