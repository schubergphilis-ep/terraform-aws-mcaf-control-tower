locals {
  create_kms_key = var.kms_key.existing_arn == null

  cloudtrail_arn                  = "arn:aws:cloudtrail:${local.home_region}:${local.management_account_id}:trail/aws-controltower-BaselineCloudTrail"
  kms_key_arn                     = local.create_kms_key ? module.control_tower_kms[0].arn : var.kms_key.existing_arn
  kms_key_iam_arns_administrative = concat(var.kms_key.iam_arns_administrative, [data.aws_iam_session_context.current.issuer_arn])
  kms_key_resource                = "arn:aws:kms:${local.home_region}:${local.management_account_id}:key/*"
}

module "control_tower_kms" {
  count = local.create_kms_key ? 1 : 0

  source  = "schubergphilis/mcaf-kms/aws"
  version = "~> 1.0.0"

  name                    = "control-tower-kms"
  description             = "Control Tower KMS key"
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.control_tower_kms[0].json
  tags                    = var.tags
}

data "aws_iam_policy_document" "control_tower_kms" {
  count = local.create_kms_key ? 1 : 0

  statement {
    sid       = "Full Permissions For Root User Only"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = [local.kms_key_resource]

    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalType"
      values   = ["Account"]
    }

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.management_account_id}:root"]
    }
  }

  statement {
    sid    = "Read/List Permissions"
    effect = "Allow"
    actions = [
      "kms:Describe*",
      "kms:ListAliases",
      "kms:ListKeys",
      "kms:GetKeyPolicy"
    ]
    resources = [local.kms_key_resource]

    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${local.management_account_id}:root"]
    }
  }

  statement {
    sid = "Administrative Permissions"
    actions = [
      "kms:CancelKeyDeletion",
      "kms:Create*",
      "kms:Decrypt",
      "kms:Delete*",
      "kms:Describe*",
      "kms:Disable*",
      "kms:Enable*",
      "kms:Encrypt",
      "kms:Get*",
      "kms:List*",
      "kms:Put*",
      "kms:ReplicateKey",
      "kms:Revoke*",
      "kms:ScheduleKeyDeletion",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:Update*"
    ]
    effect    = "Allow"
    resources = [local.kms_key_resource]

    principals {
      type        = "AWS"
      identifiers = local.kms_key_iam_arns_administrative
    }
  }


  dynamic "statement" {
    for_each = length(var.kms_key.iam_arns_decrypt) > 0 ? { create = true } : {}

    content {
      sid = "Decrypt Permissions"
      actions = [
        "kms:Decrypt"
      ]
      effect    = "Allow"
      resources = [local.kms_key_resource]

      principals {
        type        = "AWS"
        identifiers = var.kms_key.iam_arns_decrypt
      }
    }
  }

  statement {
    sid    = "Allow CloudTrail Log Group Encryption"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:Describe*",
      "kms:Encrypt",
      "kms:GenerateDataKey*",
      "kms:ReEncrypt*",
    ]
    resources = [local.kms_key_resource]

    principals {
      type        = "Service"
      identifiers = ["logs.${local.home_region}.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${local.management_account_id}:trail/*"]
    }
  }

  statement {
    sid    = "Allow CloudTrail And AWS Config To Encrypt Decrypt Logs"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = [local.kms_key_resource]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com", "config.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${local.management_account_id}:trail/*"]
    }
  }

  statement {
    sid       = "Allow Config Decrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [local.kms_key_resource]

    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }
  }

  statement {
    sid       = "Allow CloudTrail Decrypt"
    effect    = "Allow"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey*"]
    resources = [local.kms_key_resource]

    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.cloudtrail_arn]
    }

    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:aws:cloudtrail:*:${local.management_account_id}:trail/*"]
    }
  }
}
