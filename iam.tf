# AWS Control Tower requires 3 pre-existing IAM service roles in the core-management
# account before the landing zone can be created.
# ref: https://docs.aws.amazon.com/controltower/latest/userguide/access-control-managing-permissions.html
# ref: https://docs.aws.amazon.com/controltower/latest/userguide/roles-how.html


module "control_tower_admin" {
  source  = "schubergphilis/mcaf-role/aws"
  version = "~> 0.5.3"

  name                  = "AWSControlTowerAdmin"
  path                  = "/service-role/"
  postfix               = false
  principal_identifiers = ["controltower.amazonaws.com"]
  principal_type        = "Service"
  tags                  = var.tags

  policy_arns = [
    "arn:aws:iam::aws:policy/service-role/AWSControlTowerIdentityCenterManagementPolicy",
    "arn:aws:iam::aws:policy/service-role/AWSControlTowerServiceRolePolicy"
  ]
}

# Customer-managed policy created by AWS Control Tower during console setup. Modelled
# as a standalone `aws_iam_policy` + attachment (rather than the mcaf-role `role_policy`
# inline policy) so it matches the default Control Tower setup and can be imported.
resource "aws_iam_policy" "control_tower_admin" {
  name        = "AWSControlTowerAdminPolicy"
  path        = "/service-role/"
  description = "AWS Control Tower policy to manage AWS resources"
  policy      = data.aws_iam_policy_document.admin.json
  tags        = var.tags
}

resource "aws_iam_role_policy_attachment" "control_tower_admin" {
  role       = module.control_tower_admin.name
  policy_arn = aws_iam_policy.control_tower_admin.arn
}

data "aws_iam_policy_document" "admin" {
  statement {
    effect    = "Allow"
    actions   = ["ec2:DescribeAvailabilityZones"]
    resources = ["*"]
  }
}

# AWSControlTowerCloudTrailRole
module "control_tower_cloudtrail" {
  source  = "schubergphilis/mcaf-role/aws"
  version = "~> 0.5.3"

  name                  = "AWSControlTowerCloudTrail"
  path                  = "/service-role/"
  policy_arns           = ["arn:aws:iam::aws:policy/service-role/AWSControlTowerCloudTrailRolePolicy"]
  postfix               = true
  principal_identifiers = ["cloudtrail.amazonaws.com"]
  principal_type        = "Service"
  tags                  = var.tags
}

# AWSControlTowerStackSetRole
module "control_tower_stackset" {
  source  = "schubergphilis/mcaf-role/aws"
  version = "~> 0.5.3"

  name                  = "AWSControlTowerStackSet"
  path                  = "/service-role/"
  postfix               = true
  principal_identifiers = ["cloudformation.amazonaws.com"]
  principal_type        = "Service"
  tags                  = var.tags
}

# Customer-managed policy created by AWS Control Tower during console setup. Modelled
# as a standalone `aws_iam_policy` + attachment (rather than the mcaf-role `role_policy`
# inline policy) so it matches the default Control Tower setup and can be imported.
resource "aws_iam_policy" "control_tower_stackset" {
  name        = "AWSControlTowerStackSetRolePolicy"
  path        = "/service-role/"
  description = "AWS CloudFormation assumes this role to deploy stacksets in the shared AWS Control Tower accounts"
  policy      = data.aws_iam_policy_document.stackset.json
  tags        = var.tags
}

resource "aws_iam_role_policy_attachment" "control_tower_stackset" {
  role       = module.control_tower_stackset.name
  policy_arn = aws_iam_policy.control_tower_stackset.arn
}

data "aws_iam_policy_document" "stackset" {
  statement {
    effect    = "Allow"
    actions   = ["sts:AssumeRole"]
    resources = ["arn:aws:iam::*:role/AWSControlTowerExecution"]
  }
}
