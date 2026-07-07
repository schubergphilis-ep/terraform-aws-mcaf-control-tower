provider "aws" {
  region = "eu-central-1"
}

module "organization" {
  source  = "schubergphilis-ep/mcaf-organization/aws"
  version = "~> 0.3"
}

module "control_tower" {
  source = "../../"

  audit_account   = { email = "int+core-audit@example.com" }
  logging_account = { email = "int+core-logging@example.com" }

  depends_on = [module.organization]
}
