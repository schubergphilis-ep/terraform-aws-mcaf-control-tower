#!/usr/bin/env python3
"""Update AWS Control Tower accounts that show "Update available", in batches.

After a landing zone upgrade or configuration change, every enrolled account
must be updated before it is back in sync. The console does this one account
at a time via the "Update account" button, which runs through Account Factory
(AWS Service Catalog). AWS supports updating at most 5 accounts at a time:
https://docs.aws.amazon.com/controltower/latest/userguide/update-accounts-by-script.html

This script finds every Account Factory account still on an old baseline
version and re-provisions it on the latest one, 5 at a time.

Safety:
  * Dry run by default -- nothing is changed unless you pass --apply.
  * Updates re-send each account's current configuration, read from the live
    sources of truth (AWS Organizations, the product outputs, IAM Identity
    Center), so an update can never change an account's SSO user, email, name
    or OU. It only advances the baseline version. (Control Tower rejects
    UsePreviousValue, so the actual values must be sent; accounts whose
    current values cannot all be determined are skipped, never guessed.)

Run it in the Control Tower management account, in the Control Tower home
region, with Service Catalog / Account Factory admin rights.
See --help for all options.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover
    sys.exit("boto3 is required: pip install boto3")


PRODUCT_NAME = "AWS Control Tower Account Factory"

# The Account Factory product's input parameters on a landing zone with IAM
# Identity Center enabled (all versions up to and including 4.x). The actual
# keys are read from the target Account Factory version at runtime; this list
# is the fallback when that schema lookup is not permitted.
ACCOUNT_PARAM_KEYS = [
    "AccountEmail",
    "AccountName",
    "ManagedOrganizationalUnit",
    "SSOUserEmail",
    "SSOUserFirstName",
    "SSOUserLastName",
]

# AWS allows at most 5 concurrent Account Factory updates.
MAX_BATCH_SIZE = 5

# Provisioned product states that accept an update. Anything else
# (UNDER_CHANGE, ERROR, ...) would fail, so those accounts are skipped.
UPDATEABLE_STATES = {"AVAILABLE", "TAINTED"}


@dataclass
class Account:
    """An Account Factory provisioned product with an update available."""

    provisioned_product_id: str
    name: str
    state: str
    # The provisioned product's PhysicalId: a stack ARN for CFN-backed
    # products, the AWS account id for CONTROL_TOWER_ACCOUNT-type products.
    physical_id: str | None = None
    account_id: str | None = None
    ou_chain: list = field(default_factory=list)  # [(id, name), ...] up to the org root
    parameters: dict = field(default_factory=dict)  # current provisioning parameter values
    record_id: str | None = None  # Service Catalog record id of the running update
    result: str | None = None  # SUCCEEDED / FAILED / TIMED_OUT / ...
    error: str | None = None

    @property
    def ou(self) -> str:
        if self.ou_chain:
            ou_id, ou_name = self.ou_chain[0]
            return f"{ou_name} ({ou_id})"
        return "unknown"


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Batch-update Control Tower accounts showing 'Update available'.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--apply", action="store_true",
                   help="Actually perform the updates (default: dry run).")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt when using --apply.")
    p.add_argument("--region",
                   help="Control Tower home region (default: the session/profile region).")
    p.add_argument("--profile", help="AWS named profile to use.")
    p.add_argument("--batch-size", type=int, default=MAX_BATCH_SIZE,
                   help=f"Accounts to update concurrently (AWS max {MAX_BATCH_SIZE}).")
    p.add_argument("--include-ou", action="append", default=[], metavar="OU",
                   help="Only update accounts in this OU (name or id, matches nested "
                        "OUs too). Repeatable.")
    p.add_argument("--exclude-ou", action="append", default=[], metavar="OU",
                   help="Never update accounts in this OU (name or id). Repeatable; "
                        "wins over --include-ou.")
    p.add_argument("--exclude-account", action="append", default=[], metavar="ACCOUNT",
                   help="Never update this account (name or id). Repeatable.")
    p.add_argument("--product-name", default=PRODUCT_NAME,
                   help="Service Catalog product name for Account Factory.")
    p.add_argument("--param-key", action="append", dest="param_keys", metavar="KEY",
                   help="Override which provisioning parameters are re-sent (with "
                        "their current values). Repeatable.")
    p.add_argument("--poll-interval", type=int, default=30,
                   help="Seconds between status polls while a batch is updating.")
    p.add_argument("--batch-timeout", type=int, default=3600,
                   help="Max seconds to wait for one batch to finish.")

    args = p.parse_args(argv)
    if args.batch_size < 1:
        p.error("--batch-size must be at least 1")
    if args.batch_size > MAX_BATCH_SIZE:
        eprint(f"note: clamping --batch-size to the AWS maximum of {MAX_BATCH_SIZE}")
        args.batch_size = MAX_BATCH_SIZE
    return args


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def find_account_factory_product(catalog, product_name: str) -> str:
    """Return the Service Catalog product id of the Account Factory product."""
    try:  # the admin search sees every product in the catalog
        for page in catalog.get_paginator("search_products_as_admin").paginate():
            for view in page.get("ProductViewDetails", []):
                summary = view.get("ProductViewSummary", {})
                if summary.get("Name") == product_name:
                    return summary["ProductId"]
    except (ClientError, BotoCoreError):
        pass  # no admin access; fall back to the end-user search below
    found = catalog.search_products(Filters={"FullTextSearch": [product_name]})
    for view in found.get("ProductViewSummaries", []):
        if view.get("Name") == product_name:
            return view["ProductId"]
    sys.exit(f"error: no Service Catalog product named {product_name!r} found. "
             "Are you in the Control Tower management account and home region?")


def latest_baseline_version(catalog, product_id: str) -> tuple[str, str]:
    """Return (artifact_id, name) of the newest active provisioning artifact."""
    versions = catalog.list_provisioning_artifacts(
        ProductId=product_id)["ProvisioningArtifactDetails"]
    active = [v for v in versions if v.get("Active") and v.get("Guidance") != "DEPRECATED"]
    if not active:
        sys.exit("error: the Account Factory product has no active version.")
    newest = max(active, key=lambda v: v["CreatedTime"])
    return newest["Id"], newest.get("Name", newest["Id"])


def landing_zone_is_busy(controltower) -> bool:
    """True while a landing zone operation is running.

    Account updates must wait until the landing zone operation finishes, per
    the AWS guidance on updating accounts after a landing zone update.
    """
    operations = controltower.list_landing_zone_operations(
        filter={"statuses": ["IN_PROGRESS"]}).get("landingZoneOperations", [])
    return bool(operations)


def required_parameter_keys(catalog, product_id: str, artifact_id: str) -> list[str]:
    """The parameter keys declared by the target Account Factory version.

    Asking Service Catalog -- instead of assuming the well-known six keys --
    keeps the script correct across landing zone versions: AWS owns this
    schema and it can differ per setup and version.
    """
    kwargs = {"ProductId": product_id, "ProvisioningArtifactId": artifact_id}
    paths = catalog.list_launch_paths(ProductId=product_id).get("LaunchPathSummaries", [])
    if paths:
        kwargs["PathId"] = paths[0]["Id"]
    declared = catalog.describe_provisioning_parameters(**kwargs).get(
        "ProvisioningArtifactParameters", [])
    return [parameter["ParameterKey"] for parameter in declared]


def accounts_needing_update(catalog, product_id: str, latest_artifact_id: str) -> list[Account]:
    """All Account Factory accounts still on an older baseline version.

    boto3 has no paginator for SearchProvisionedProducts, so we page manually.
    """
    accounts: list[Account] = []
    page_token = None
    while True:
        kwargs = {"AccessLevelFilter": {"Key": "Account", "Value": "self"}, "PageSize": 100}
        if page_token:
            kwargs["PageToken"] = page_token
        page = catalog.search_provisioned_products(**kwargs)
        for product in page.get("ProvisionedProducts", []):
            if (product.get("ProductId") == product_id
                    and product.get("ProvisioningArtifactId") != latest_artifact_id):
                accounts.append(Account(
                    provisioned_product_id=product["Id"],
                    name=product.get("Name", product["Id"]),
                    state=product.get("Status", "unknown"),
                    physical_id=product.get("PhysicalId"),
                ))
        page_token = page.get("NextPageToken")
        if not page_token:
            return sorted(accounts, key=lambda a: a.name)


def resolve_account_details(catalog, cloudformation, org, sso: SsoDirectory,
                            accounts: list[Account]) -> None:
    """Best effort: look up each account's id, OU ancestry, and current parameters.

    The full OU chain up to the org root is kept so the OU filters also match
    accounts in nested OUs (e.g. --exclude-ou Workloads covers Workloads/Sandbox).
    Lookups that fail leave the detail unknown, with a warning saying why;
    select_accounts() then skips accounts that can't be updated safely.
    """
    ou_names: dict[str, str] = {}
    for account in accounts:
        outputs: dict[str, str] = {}
        try:
            found = catalog.get_provisioned_product_outputs(
                ProvisionedProductId=account.provisioned_product_id)["Outputs"]
            outputs = {o["OutputKey"]: o["OutputValue"] for o in found if o.get("OutputKey")}
            account.account_id = outputs.get("AccountId")
        except (ClientError, BotoCoreError) as exc:
            eprint(f"  ! {account.name}: could not read product outputs: {error_text(exc)}")
        physical = account.physical_id or ""
        if not account.account_id and len(physical) == 12 and physical.isdigit():
            # CONTROL_TOWER_ACCOUNT-type products carry the account id as the
            # PhysicalId (as used by AWS's own Control Tower reference code).
            account.account_id = physical

        try:
            if account.account_id:
                account.ou_chain = ou_ancestry(org, account.account_id, ou_names)
        except (ClientError, BotoCoreError) as exc:
            # e.g. ChildNotFoundException means the account is no longer in the
            # organization, so its Account Factory record is probably stale.
            code = error_text(exc).split(":")[0]
            hint = (" (account no longer in the organization? Its Account Factory "
                    "record may be stale)" if code == "ChildNotFoundException" else "")
            eprint(f"  ! {account.name}: could not look up its OU: {error_text(exc)}{hint}")

        account.parameters = gather_parameters(cloudformation, org, sso, account, outputs)


def error_text(exc: Exception) -> str:
    """The AWS error code and message of a ClientError, or the exception text."""
    error = getattr(exc, "response", {}).get("Error", {})
    if error.get("Code"):
        return f"{error['Code']}: {error.get('Message', '')}".rstrip(": ")
    return str(exc)


class SsoDirectory:
    """Looks up IAM Identity Center users' first/last names by email."""

    def __init__(self, sso_admin, identitystore):
        self._sso_admin = sso_admin
        self._identitystore = identitystore
        self._store_id: str | None = None
        self._cache: dict[str, tuple[str, str]] = {}

    def names(self, email: str) -> tuple[str, str]:
        """Return (first name, last name) for the user, or ("", "") if unknown."""
        if email not in self._cache:
            self._cache[email] = self._look_up(email)
        return self._cache[email]

    def _look_up(self, email: str) -> tuple[str, str]:
        if self._store_id is None:
            instances = self._sso_admin.list_instances().get("Instances", [])
            self._store_id = instances[0]["IdentityStoreId"] if instances else ""
        if not self._store_id:
            return "", ""
        users = self._identitystore.list_users(
            IdentityStoreId=self._store_id,
            Filters=[{"AttributePath": "UserName", "AttributeValue": email}],
        ).get("Users", [])
        if not users:
            return "", ""
        name = users[0].get("Name", {})
        return name.get("GivenName", ""), name.get("FamilyName", "")


def gather_parameters(cloudformation, org, sso: SsoDirectory, account: Account,
                      outputs: dict[str, str]) -> dict[str, str]:
    """The account's current provisioning parameter values.

    Control Tower's Account Factory rejects UsePreviousValue, so an update must
    re-send the actual current values. Live sources of truth take precedence:
    AWS Organizations for the account email and name, the resolved OU for
    ManagedOrganizationalUnit, the product outputs for SSOUserEmail, and IAM
    Identity Center for the SSO user's names. The provisioned product's
    CloudFormation stack fills in the rest when the product is CFN-backed;
    CONTROL_TOWER_ACCOUNT-type products have no stack to read.
    """
    values: dict[str, str] = {}

    # Only CFN-backed provisioned products have a stack to read;
    # CONTROL_TOWER_ACCOUNT-type products carry the account id as PhysicalId.
    if account.physical_id and account.physical_id.startswith("arn:"):
        try:
            stack = cloudformation.describe_stacks(StackName=account.physical_id)["Stacks"][0]
            for parameter in stack.get("Parameters", []):
                if parameter.get("ParameterValue") not in (None, "****"):  # **** = NoEcho
                    values[parameter["ParameterKey"]] = parameter["ParameterValue"]
        except (ClientError, BotoCoreError) as exc:
            eprint(f"  ! {account.name}: could not read the product's stack: {error_text(exc)}")

    for key in ("AccountEmail", "SSOUserEmail"):
        if not values.get(key) and outputs.get(key):
            values[key] = outputs[key]

    if account.account_id:
        try:
            info = org.describe_account(AccountId=account.account_id)["Account"]
            values["AccountEmail"] = info["Email"]
            values["AccountName"] = info["Name"]
        except (ClientError, BotoCoreError) as exc:
            eprint(f"  ! {account.name}: could not read the account from "
                   f"Organizations: {error_text(exc)}")

    if account.ou_chain:
        ou_id, ou_name = account.ou_chain[0]
        values["ManagedOrganizationalUnit"] = f"{ou_name} ({ou_id})"

    sso_email = values.get("SSOUserEmail")
    if sso_email and not (values.get("SSOUserFirstName") and values.get("SSOUserLastName")):
        try:
            first, last = sso.names(sso_email)
            values.setdefault("SSOUserFirstName", first)
            values.setdefault("SSOUserLastName", last)
        except (ClientError, BotoCoreError) as exc:
            eprint(f"  ! {account.name}: could not look up the SSO user in "
                   f"Identity Center: {error_text(exc)}")

    return {key: value for key, value in values.items() if value}


def ou_ancestry(org, account_id: str, ou_names: dict[str, str]) -> list[tuple[str, str]]:
    """Walk from the account up to the org root; return [(id, name), ...]."""
    chain: list[tuple[str, str]] = []
    child = account_id
    while True:
        parents = org.list_parents(ChildId=child).get("Parents", [])
        if not parents:
            return chain
        parent = parents[0]
        if parent["Type"] != "ORGANIZATIONAL_UNIT":  # reached the org root
            chain.append((parent["Id"], "Root"))
            return chain
        ou_id = parent["Id"]
        if ou_id not in ou_names:
            ou_names[ou_id] = org.describe_organizational_unit(
                OrganizationalUnitId=ou_id)["OrganizationalUnit"]["Name"]
        chain.append((ou_id, ou_names[ou_id]))
        child = ou_id


def in_any_ou(account: Account, selectors: list[str]) -> bool:
    """True if any ancestor OU matches a selector, by id or (case-insensitive) name."""
    for ou_id, ou_name in account.ou_chain:
        for wanted in selectors:
            if wanted == ou_id or wanted.lower() == ou_name.lower():
                return True
    return False


def select_accounts(accounts: list[Account], args: argparse.Namespace,
                    param_keys: list[str]) -> list[Account]:
    """Apply the account/OU filters and drop accounts that can't be updated safely."""
    if args.exclude_account:
        excluded = {selector.lower() for selector in args.exclude_account}
        kept = []
        for account in accounts:
            if account.name.lower() in excluded or (account.account_id or "") in excluded:
                eprint(f"  ! skipping {account.name}: excluded by --exclude-account")
            else:
                kept.append(account)
        accounts = kept

    if args.include_ou or args.exclude_ou:
        for account in accounts:
            if not account.ou_chain:
                eprint(f"  ! skipping {account.name}: OU unknown -- excluded to be "
                       "safe while OU filters are active")
        accounts = [a for a in accounts if a.ou_chain]
        if args.include_ou:
            accounts = [a for a in accounts if in_any_ou(a, args.include_ou)]
        if args.exclude_ou:
            accounts = [a for a in accounts if not in_any_ou(a, args.exclude_ou)]

    ready = []
    for account in accounts:
        if account.state not in UPDATEABLE_STATES:
            eprint(f"  ! skipping {account.name}: state is {account.state} -- "
                   "retry once it settles")
            continue
        # Updating without the current values could change the account's
        # configuration, so never guess: skip when any value is unknown.
        missing = [key for key in param_keys if not account.parameters.get(key)]
        if missing:
            eprint(f"  ! skipping {account.name}: current value unknown for "
                   f"{', '.join(missing)}")
            continue
        ready.append(account)
    return ready


def start_update(catalog, account: Account, product_id: str, artifact_id: str,
                 param_keys: list[str]) -> None:
    """Kick off the asynchronous update for one account.

    The account's current parameter values are re-sent unchanged. Control Tower
    rejects UsePreviousValue, and sending different values could change the
    account's email, SSO user or OU.
    """
    response = catalog.update_provisioned_product(
        ProvisionedProductId=account.provisioned_product_id,
        ProductId=product_id,
        ProvisioningArtifactId=artifact_id,
        ProvisioningParameters=[
            {"Key": key, "Value": account.parameters[key]} for key in param_keys],
    )
    account.record_id = response["RecordDetail"]["RecordId"]
    account.result = response["RecordDetail"].get("Status", "CREATED")


def wait_for_batch(catalog, batch: list[Account], poll_interval: int, timeout: int) -> None:
    """Poll the submitted updates until they all succeed, fail, or time out."""
    pending = [a for a in batch if a.record_id]
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        time.sleep(poll_interval)
        still_running = []
        for account in pending:
            try:
                record = catalog.describe_record(Id=account.record_id)["RecordDetail"]
            except (ClientError, BotoCoreError) as exc:
                account.result, account.error = "FAILED", f"could not read status: {exc}"
                print(f"  ✗ {account.name}: {account.error}")
                continue
            account.result = record.get("Status", "IN_PROGRESS")
            if account.result == "SUCCEEDED":
                print(f"  ✓ {account.name}")
            elif account.result == "FAILED":
                account.error = "; ".join(
                    e.get("Description", "") for e in record.get("RecordErrors", []))
                print(f"  ✗ {account.name}: {account.error or 'failed'}")
            else:  # CREATED / IN_PROGRESS / IN_PROGRESS_IN_ERROR (rolling back)
                still_running.append(account)
        pending = still_running
    for account in pending:
        account.result = "TIMED_OUT"
        account.error = "timed out waiting for the update to finish"
        print(f"  ? {account.name}: {account.error}")


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if not session.region_name:
        eprint("error: no region configured. Pass --region or set a default region.")
        return 2
    retries = Config(retries={"max_attempts": 10, "mode": "adaptive"})
    catalog = session.client("servicecatalog", config=retries)
    cloudformation = session.client("cloudformation", config=retries)
    org = session.client("organizations", config=retries)
    sso = SsoDirectory(session.client("sso-admin", config=retries),
                       session.client("identitystore", config=retries))
    identity = session.client("sts", config=retries).get_caller_identity()

    print(f"Account : {identity['Account']}")
    print(f"Identity: {identity['Arn']}")
    print(f"Region  : {session.region_name}")
    print(f"Mode    : {'APPLY -- accounts will be updated' if args.apply else 'dry run'}")
    print()

    try:
        busy = landing_zone_is_busy(session.client("controltower", config=retries))
    except (ClientError, BotoCoreError):
        busy = False  # can't tell; Control Tower will reject updates itself if busy
    if busy:
        eprint("! A landing zone operation is still in progress. Account updates "
               "can only start after it finishes.")
        if args.apply:
            return 1

    product_id = find_account_factory_product(catalog, args.product_name)
    artifact_id, artifact_name = latest_baseline_version(catalog, product_id)
    print(f"Latest baseline version: {artifact_name} ({artifact_id})")

    if args.param_keys:
        param_keys = args.param_keys
    else:
        try:
            param_keys = required_parameter_keys(catalog, product_id, artifact_id)
        except (ClientError, BotoCoreError) as exc:
            eprint(f"note: could not read the parameter schema "
                   f"({error_text(exc)}); assuming the standard parameters")
            param_keys = []
        param_keys = param_keys or ACCOUNT_PARAM_KEYS
    print(f"Parameters re-sent on update: {', '.join(param_keys)}")
    print()

    accounts = accounts_needing_update(catalog, product_id, artifact_id)
    resolve_account_details(catalog, cloudformation, org, sso, accounts)
    accounts = select_accounts(accounts, args, param_keys)
    if not accounts:
        print("No accounts have an update available. Nothing to do.")
        return 0

    print(f"{len(accounts)} account(s) with an update available:")
    print()
    print(f"  {'ACCOUNT NAME':<32} {'ACCOUNT ID':<14} OU")
    for account in accounts:
        print(f"  {account.name:<32} {account.account_id or '-':<14} {account.ou}")
    print()

    if not args.apply:
        print("Dry run: re-run with --apply to update these accounts.")
        return 0
    if not args.yes:
        answer = input(f"Update {len(accounts)} account(s) in batches of "
                       f"{args.batch_size}? Type 'yes' to proceed: ")
        if answer.strip().lower() != "yes":
            print("Aborted; no changes made.")
            return 1

    batches = [accounts[i:i + args.batch_size]
               for i in range(0, len(accounts), args.batch_size)]
    for number, batch in enumerate(batches, start=1):
        print(f"\nBatch {number}/{len(batches)}: updating {len(batch)} account(s)...")
        for account in batch:
            try:
                start_update(catalog, account, product_id, artifact_id, param_keys)
                print(f"  → {account.name}: update started")
            except (ClientError, BotoCoreError) as exc:
                account.result, account.error = "FAILED", str(exc)
                eprint(f"  ✗ {account.name}: could not start update: {exc}")
        wait_for_batch(catalog, batch, args.poll_interval, args.batch_timeout)

    failed = [a for a in accounts if a.result != "SUCCEEDED"]
    print(f"\nDone: {len(accounts) - len(failed)} succeeded, {len(failed)} failed.")
    for account in failed:
        print(f"  - {account.name} [{account.result}] {account.error or ''}".rstrip())
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        eprint("\nInterrupted. Updates already submitted keep running in AWS; "
               "re-run the script to see what still needs updating.")
        sys.exit(130)
