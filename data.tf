locals {
  home_region           = data.aws_region.current.region
  management_account_id = data.aws_caller_identity.current.account_id
}

data "aws_organizations_organization" "current" {}

data "aws_caller_identity" "current" {}

data "aws_iam_session_context" "current" {
  arn = data.aws_caller_identity.current.arn
}

data "aws_region" "current" {}
