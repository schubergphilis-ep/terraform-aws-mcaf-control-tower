# Migrating an existing setup to this module

This guide explains how to bring an existing (console-created) Control Tower landing zone under this module's management using `terraform import`.

> [!IMPORTANT]
> Always review the full plan output **before** applying. A wrong import (especially of an `aws_organizations_account`) can be very expensive to undo.

## Landing zone v4.0 requirement

This module requires the **v4.0 manifest schema** or higher. Before importing:

1. **Upgrade your landing zone.** In the Control Tower console go to **Settings → Update landing zone** and select the latest 4.x version. Wait until the status shows `SUCCEEDED` (30–90 minutes).
2. **Resolve any drift.** In the Control Tower console go to **Organization** and update every account that shows drift.

## Collect identifiers

Run these commands from a shell authenticated to the management account:

```sh
# Core account IDs
aws organizations list-accounts --query 'Accounts[?Name==`core-audit`].Id'   --output text
aws organizations list-accounts --query 'Accounts[?Name==`core-logging`].Id' --output text

# OU that holds the core accounts (pass this as core_accounts_parent_id)
aws organizations list-parents --child-id <AUDIT_ACCOUNT_ID> --query 'Parents[0].Id' --output text

# KMS key used by Control Tower (adjust the alias to match yours)
aws kms describe-key --key-id alias/control-tower-kms --query 'KeyMetadata.{Arn:Arn,KeyId:KeyId}'

# Landing zone ARN
aws controltower list-landing-zones --query 'landingZones[0].arn' --output text
```

Keep these values — you'll use them in the import blocks below.

## Import blocks

Replace the placeholder values (`<...>`) with the identifiers you collected above.

```hcl
import {
  to = module.control_tower.aws_organizations_organizational_unit.default["security"]
  id = "<SECURITY_OU_ID>"  # e.g. "ou-xxxx-xxxxxxxx"
}

import {
  to = module.control_tower.aws_organizations_account.audit
  id = "<AUDIT_ACCOUNT_ID>"  # e.g. "123456789012"
}

import {
  to = module.control_tower.aws_organizations_account.logging
  id = "<LOGGING_ACCOUNT_ID>"  # e.g. "123456789013"
}

import {
  to = module.control_tower.aws_controltower_landing_zone.default
  id = "<LANDING_ZONE_ARN>"  # e.g. "arn:aws:controltower:<REGION>:<MGMT_ACCOUNT_ID>:landingzone/<LZ_ID>"
}

import {
  to = module.control_tower.module.control_tower_admin.aws_iam_role.default
  id = "AWSControlTowerAdmin"
}

import {
  to = module.control_tower.module.control_tower_admin.aws_iam_role_policy_attachment.default["arn:aws:iam::aws:policy/service-role/AWSControlTowerIdentityCenterManagementPolicy"]
  id = "AWSControlTowerAdmin/arn:aws:iam::aws:policy/service-role/AWSControlTowerIdentityCenterManagementPolicy"
}

import {
  to = module.control_tower.module.control_tower_admin.aws_iam_role_policy_attachment.default["arn:aws:iam::aws:policy/service-role/AWSControlTowerServiceRolePolicy"]
  id = "AWSControlTowerAdmin/arn:aws:iam::aws:policy/service-role/AWSControlTowerServiceRolePolicy"
}

import {
  to = module.control_tower.aws_iam_policy.control_tower_admin
  id = "arn:aws:iam::<MGMT_ACCOUNT_ID>:policy/service-role/AWSControlTowerAdminPolicy"
}

import {
  to = module.control_tower.aws_iam_role_policy_attachment.control_tower_admin
  id = "AWSControlTowerAdmin/arn:aws:iam::<MGMT_ACCOUNT_ID>:policy/service-role/AWSControlTowerAdminPolicy"
}

import {
  to = module.control_tower.module.control_tower_cloudtrail.aws_iam_role.default
  id = "AWSControlTowerCloudTrailRole"
}

import {
  to = module.control_tower.module.control_tower_cloudtrail.aws_iam_role_policy_attachment.default["arn:aws:iam::aws:policy/service-role/AWSControlTowerCloudTrailRolePolicy"]
  id = "AWSControlTowerCloudTrailRole/arn:aws:iam::aws:policy/service-role/AWSControlTowerCloudTrailRolePolicy"
}

import {
  to = module.control_tower.module.control_tower_stackset.aws_iam_role.default
  id = "AWSControlTowerStackSetRole"
}

import {
  to = module.control_tower.aws_iam_policy.control_tower_stackset
  id = "arn:aws:iam::<MGMT_ACCOUNT_ID>:policy/service-role/AWSControlTowerStackSetRolePolicy"
}

import {
  to = module.control_tower.aws_iam_role_policy_attachment.control_tower_stackset
  id = "AWSControlTowerStackSetRole/arn:aws:iam::<MGMT_ACCOUNT_ID>:policy/service-role/AWSControlTowerStackSetRolePolicy"
}
```

## Expected diffs after import

After importing, the only planned change should be **`tags`**. If you see other changes, verify your variable values match the existing configuration.
