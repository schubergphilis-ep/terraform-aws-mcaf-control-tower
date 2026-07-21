"""Tests for update_control_tower_accounts.py.

All AWS calls are stubbed, so the tests run offline and never touch an account.

Run them with either:
    python3 -m unittest discover -s scripts -v
    pytest scripts/
"""

from __future__ import annotations

import io
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import update_control_tower_accounts as script  # noqa: E402


def access_denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, operation)


def account(name: str = "acct", state: str = "AVAILABLE", **kwargs) -> script.Account:
    return script.Account(provisioned_product_id=f"pp-{name}", name=name, state=state, **kwargs)


def ou_args(include=(), exclude=(), exclude_account=()) -> Namespace:
    return Namespace(include_ou=list(include), exclude_ou=list(exclude),
                     exclude_account=list(exclude_account))


# An account in Workloads/Sandbox, mirroring a nested OU structure.
NESTED_CHAIN = [
    ("ou-m3lh-ddvurul0", "Sandbox"),
    ("ou-m3lh-6bhwnef5", "Workloads"),
    ("r-m3lh", "Root"),
]

# A full set of current parameter values, as read from a CloudFormation stack.
STACK_PARAMS = {
    "AccountEmail": "account+test@example.com",
    "AccountName": "test-account",
    "ManagedOrganizationalUnit": "Sandbox (ou-m3lh-ddvurul0)",
    "SSOUserEmail": "sso@example.com",
    "SSOUserFirstName": "AWS",
    "SSOUserLastName": "Control Tower Admin",
}


class StackCfn:
    """CloudFormation stub whose stack returns the given parameter values."""

    def __init__(self, values=STACK_PARAMS):
        self.values = values

    def describe_stacks(self, StackName):
        return {"Stacks": [{"Parameters": [
            {"ParameterKey": key, "ParameterValue": value}
            for key, value in self.values.items()]}]}


class GoneCfn:
    """CloudFormation stub for provisioned products without a readable stack
    (normal for accounts updated by newer Control Tower versions)."""

    def describe_stacks(self, StackName):
        raise ClientError({"Error": {"Code": "ValidationError",
                                     "Message": f"Stack with id {StackName} does not exist"}},
                          "DescribeStacks")


class OrgInfo:
    """Organizations stub that knows the account's current email and name."""

    def __init__(self, email=STACK_PARAMS["AccountEmail"], name=STACK_PARAMS["AccountName"]):
        self.email, self.name = email, name

    def describe_account(self, AccountId):
        return {"Account": {"Id": AccountId, "Email": self.email, "Name": self.name}}


class FakeSso:
    """Identity Center stub returning fixed names, counting lookups."""

    def __init__(self, first="AWS", last="Control Tower Admin"):
        self.first, self.last = first, last
        self.calls = 0

    def names(self, email):
        self.calls += 1
        return self.first, self.last


class TestParseArgs(unittest.TestCase):
    def test_defaults_are_a_dry_run_in_batches_of_five(self):
        args = script.parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.yes)
        self.assertEqual(args.batch_size, 5)
        self.assertEqual(args.include_ou, [])
        self.assertEqual(args.exclude_ou, [])

    def test_all_flags_are_parsed(self):
        args = script.parse_args([
            "--apply", "--yes", "--batch-size", "3",
            "--include-ou", "Sandbox", "--exclude-ou", "Security",
            "--exclude-account", "sandbox",
            "--param-key", "AccountEmail", "--poll-interval", "10", "--batch-timeout", "60",
        ])
        self.assertTrue(args.apply and args.yes)
        self.assertEqual(args.batch_size, 3)
        self.assertEqual(args.include_ou, ["Sandbox"])
        self.assertEqual(args.exclude_ou, ["Security"])
        self.assertEqual(args.exclude_account, ["sandbox"])
        self.assertEqual(args.param_keys, ["AccountEmail"])

    def test_batch_size_is_clamped_to_the_aws_maximum(self):
        with redirect_stderr(io.StringIO()):
            args = script.parse_args(["--batch-size", "9"])
        self.assertEqual(args.batch_size, script.MAX_BATCH_SIZE)

    def test_batch_size_below_one_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            script.parse_args(["--batch-size", "0"])
        self.assertEqual(ctx.exception.code, 2)


class TestFindAccountFactoryProduct(unittest.TestCase):
    def test_found_via_the_admin_search(self):
        class Catalog:
            def get_paginator(self, name):
                assert name == "search_products_as_admin"
                class Paginator:
                    def paginate(self):
                        return [{"ProductViewDetails": [
                            {"ProductViewSummary": {"Name": "Other", "ProductId": "prod-x"}},
                            {"ProductViewSummary": {"Name": script.PRODUCT_NAME, "ProductId": "prod-1"}},
                        ]}]
                return Paginator()

        self.assertEqual(script.find_account_factory_product(Catalog(), script.PRODUCT_NAME), "prod-1")

    def test_falls_back_to_the_end_user_search_when_admin_access_is_denied(self):
        class Catalog:
            def get_paginator(self, name):
                raise access_denied("SearchProductsAsAdmin")
            def search_products(self, Filters):
                return {"ProductViewSummaries": [{"Name": script.PRODUCT_NAME, "ProductId": "prod-2"}]}

        self.assertEqual(script.find_account_factory_product(Catalog(), script.PRODUCT_NAME), "prod-2")

    def test_exits_with_a_helpful_error_when_the_product_is_missing(self):
        class Catalog:
            def get_paginator(self, name):
                raise access_denied("SearchProductsAsAdmin")
            def search_products(self, Filters):
                return {"ProductViewSummaries": []}

        with self.assertRaises(SystemExit) as ctx:
            script.find_account_factory_product(Catalog(), script.PRODUCT_NAME)
        self.assertIn("management account", str(ctx.exception))


class TestLatestBaselineVersion(unittest.TestCase):
    def test_picks_the_newest_active_non_deprecated_version(self):
        class Catalog:
            def list_provisioning_artifacts(self, ProductId):
                return {"ProvisioningArtifactDetails": [
                    {"Id": "pa-1", "Name": "v1", "Active": True, "CreatedTime": datetime(2024, 1, 1)},
                    {"Id": "pa-2", "Name": "v2", "Active": True, "Guidance": "DEPRECATED",
                     "CreatedTime": datetime(2025, 1, 1)},
                    {"Id": "pa-3", "Name": "v3", "Active": False, "CreatedTime": datetime(2026, 1, 1)},
                    {"Id": "pa-4", "Name": "v4", "Active": True, "CreatedTime": datetime(2025, 6, 1)},
                ]}

        self.assertEqual(script.latest_baseline_version(Catalog(), "prod-1"), ("pa-4", "v4"))

    def test_exits_when_there_is_no_active_version(self):
        class Catalog:
            def list_provisioning_artifacts(self, ProductId):
                return {"ProvisioningArtifactDetails": [
                    {"Id": "pa-1", "Name": "v1", "Active": False, "CreatedTime": datetime(2024, 1, 1)},
                ]}

        with self.assertRaises(SystemExit):
            script.latest_baseline_version(Catalog(), "prod-1")


class TestRequiredParameterKeys(unittest.TestCase):
    def test_reads_the_declared_keys_via_the_launch_path(self):
        class Catalog:
            def list_launch_paths(self, ProductId):
                assert ProductId == "prod-1"
                return {"LaunchPathSummaries": [{"Id": "lp-1"}]}
            def describe_provisioning_parameters(self, **kwargs):
                assert kwargs == {"ProductId": "prod-1",
                                  "ProvisioningArtifactId": "pa-new", "PathId": "lp-1"}
                return {"ProvisioningArtifactParameters": [
                    {"ParameterKey": key} for key in script.ACCOUNT_PARAM_KEYS]}

        keys = script.required_parameter_keys(Catalog(), "prod-1", "pa-new")
        self.assertEqual(keys, script.ACCOUNT_PARAM_KEYS)

    def test_works_without_a_launch_path(self):
        class Catalog:
            def list_launch_paths(self, ProductId):
                return {"LaunchPathSummaries": []}
            def describe_provisioning_parameters(self, **kwargs):
                assert "PathId" not in kwargs
                return {"ProvisioningArtifactParameters": [{"ParameterKey": "AccountEmail"}]}

        keys = script.required_parameter_keys(Catalog(), "prod-1", "pa-new")
        self.assertEqual(keys, ["AccountEmail"])


class TestAccountsNeedingUpdate(unittest.TestCase):
    def test_follows_pagination_filters_by_product_and_version_and_sorts(self):
        class Catalog:
            calls = 0
            def search_provisioned_products(self, **kwargs):
                Catalog.calls += 1
                if Catalog.calls == 1:
                    assert "PageToken" not in kwargs
                    return {"ProvisionedProducts": [
                        # needs an update
                        {"Id": "pp-b", "Name": "bbb", "ProductId": "prod-1",
                         "ProvisioningArtifactId": "pa-old", "Status": "AVAILABLE"},
                        # different product: ignored
                        {"Id": "pp-x", "Name": "xxx", "ProductId": "prod-other",
                         "ProvisioningArtifactId": "pa-old", "Status": "AVAILABLE"},
                        # already on the latest version: ignored
                        {"Id": "pp-c", "Name": "ccc", "ProductId": "prod-1",
                         "ProvisioningArtifactId": "pa-new", "Status": "AVAILABLE"},
                    ], "NextPageToken": "tok"}
                assert kwargs.get("PageToken") == "tok"
                return {"ProvisionedProducts": [
                    {"Id": "pp-a", "Name": "aaa", "ProductId": "prod-1",
                     "ProvisioningArtifactId": "pa-old", "Status": "UNDER_CHANGE",
                     "PhysicalId": "arn:aws:cloudformation:eu-central-1:1:stack/SC-a/x"},
                ]}

        accounts = script.accounts_needing_update(Catalog(), "prod-1", "pa-new")
        self.assertEqual(Catalog.calls, 2)
        self.assertEqual([a.name for a in accounts], ["aaa", "bbb"])
        self.assertEqual(accounts[0].physical_id,
                         "arn:aws:cloudformation:eu-central-1:1:stack/SC-a/x")


class TestResolveAccountDetails(unittest.TestCase):
    class Org(OrgInfo):
        def __init__(self):
            super().__init__()
            self.describe_calls = 0
        def list_parents(self, ChildId):
            tree = {
                "329599626798": [{"Id": "ou-sand", "Type": "ORGANIZATIONAL_UNIT"}],
                "ou-sand": [{"Id": "ou-work", "Type": "ORGANIZATIONAL_UNIT"}],
                "ou-work": [{"Id": "r-m3lh", "Type": "ROOT"}],
            }
            return {"Parents": tree[ChildId]}
        def describe_organizational_unit(self, OrganizationalUnitId):
            self.describe_calls += 1
            names = {"ou-sand": "Sandbox", "ou-work": "Workloads"}
            return {"OrganizationalUnit": {"Name": names[OrganizationalUnitId]}}

    class Catalog:
        def get_provisioned_product_outputs(self, ProvisionedProductId):
            return {"Outputs": [{"OutputKey": "AccountId", "OutputValue": "329599626798"}]}

    def test_resolves_the_ou_chain_and_current_parameters(self):
        acct = account("nested", physical_id="arn:aws:cloudformation:eu-central-1:1:stack/SC-a/x")
        script.resolve_account_details(self.Catalog(), StackCfn(), self.Org(), FakeSso(), [acct])
        self.assertEqual(acct.account_id, "329599626798")
        self.assertEqual(acct.ou_chain,
                         [("ou-sand", "Sandbox"), ("ou-work", "Workloads"), ("r-m3lh", "Root")])
        self.assertEqual(acct.ou, "Sandbox (ou-sand)")
        # The resolved (live) OU wins over the value stored in the stack.
        expected = dict(STACK_PARAMS, ManagedOrganizationalUnit="Sandbox (ou-sand)")
        self.assertEqual(acct.parameters, expected)

    def test_ou_names_are_cached_across_accounts(self):
        org = self.Org()
        script.resolve_account_details(self.Catalog(), StackCfn(), org, FakeSso(),
                                       [account("one"), account("two")])
        self.assertEqual(org.describe_calls, 2)  # Sandbox + Workloads, looked up once each

    def test_lookup_failures_leave_details_unknown_and_warn_with_the_reason(self):
        class BrokenCatalog:
            def get_provisioned_product_outputs(self, ProvisionedProductId):
                raise access_denied("GetProvisionedProductOutputs")

        acct = account("broken")
        with redirect_stderr(io.StringIO()) as stderr:
            script.resolve_account_details(BrokenCatalog(), StackCfn(), self.Org(),
                                           FakeSso(), [acct])
        self.assertIsNone(acct.account_id)
        self.assertEqual(acct.ou_chain, [])
        self.assertEqual(acct.ou, "unknown")
        self.assertIn("AccessDeniedException", stderr.getvalue())

    def test_the_physical_id_provides_the_account_id_for_newer_products(self):
        class NoOutputs:
            def get_provisioned_product_outputs(self, ProvisionedProductId):
                return {"Outputs": []}

        acct = account("modern", physical_id="329599626798")
        script.resolve_account_details(NoOutputs(), StackCfn(), self.Org(), FakeSso(), [acct])
        self.assertEqual(acct.account_id, "329599626798")
        self.assertEqual(acct.ou, "Sandbox (ou-sand)")

    def test_an_account_gone_from_the_org_warns_about_a_stale_record(self):
        class GoneOrg(OrgInfo):
            def list_parents(self, ChildId):
                raise ClientError({"Error": {"Code": "ChildNotFoundException",
                                             "Message": "no such child"}}, "ListParents")

        acct = account("stale")
        with redirect_stderr(io.StringIO()) as stderr:
            script.resolve_account_details(self.Catalog(), StackCfn(), GoneOrg(),
                                           FakeSso(), [acct])
        self.assertEqual(acct.account_id, "329599626798")  # outputs still resolved
        self.assertEqual(acct.ou, "unknown")
        self.assertIn("ChildNotFoundException", stderr.getvalue())
        self.assertIn("stale", stderr.getvalue())


class TestGatherParameters(unittest.TestCase):
    def gather(self, cfn=None, org=None, sso=None, acct=None, outputs=None):
        return script.gather_parameters(
            cfn or StackCfn(), org or OrgInfo(), sso or FakeSso(),
            acct if acct is not None else account(
                "a", physical_id="arn:aws:cloudformation:eu-central-1:1:stack/SC-a/x",
                account_id="329599626798", ou_chain=list(NESTED_CHAIN)),
            outputs if outputs is not None else {})

    def test_a_readable_stack_provides_the_baseline_values(self):
        self.assertEqual(self.gather(), STACK_PARAMS)

    def test_works_without_a_stack_by_using_the_live_sources(self):
        # Newer Control Tower versions keep no readable CloudFormation stack,
        # so everything must come from Organizations, the outputs, the resolved
        # OU, and Identity Center.
        values = self.gather(cfn=GoneCfn(),
                             outputs={"SSOUserEmail": STACK_PARAMS["SSOUserEmail"]})
        self.assertEqual(values, STACK_PARAMS)

    def test_organizations_overrides_stale_stack_values(self):
        org = OrgInfo(email="renamed@example.com", name="renamed-account")
        values = self.gather(org=org)
        self.assertEqual(values["AccountEmail"], "renamed@example.com")
        self.assertEqual(values["AccountName"], "renamed-account")

    def test_the_resolved_ou_overrides_the_stack_value(self):
        stale = dict(STACK_PARAMS, ManagedOrganizationalUnit="OldOU (ou-old)")
        values = self.gather(cfn=StackCfn(stale))
        self.assertEqual(values["ManagedOrganizationalUnit"], "Sandbox (ou-m3lh-ddvurul0)")

    def test_sso_names_from_the_stack_win_over_identity_center(self):
        sso = FakeSso(first="Different", last="Person")
        values = self.gather(sso=sso)
        self.assertEqual(values["SSOUserFirstName"], STACK_PARAMS["SSOUserFirstName"])
        self.assertEqual(sso.calls, 0)  # not consulted when the stack has the names

    def test_unknown_sso_names_leave_the_keys_absent(self):
        values = self.gather(cfn=GoneCfn(), sso=FakeSso(first="", last=""),
                             outputs={"SSOUserEmail": "sso@example.com"})
        self.assertNotIn("SSOUserFirstName", values)  # select_accounts will skip

    def test_newer_products_never_query_cloudformation(self):
        # Newer Control Tower versions set the account id (not a stack ARN) as
        # the PhysicalId -- there is no stack to describe.
        class MustNotBeCalled:
            def describe_stacks(self, StackName):
                raise AssertionError("describe_stacks must not be called")

        acct = account("a", physical_id="329599626798", account_id="329599626798",
                       ou_chain=list(NESTED_CHAIN))
        values = self.gather(cfn=MustNotBeCalled(), acct=acct,
                             outputs={"SSOUserEmail": STACK_PARAMS["SSOUserEmail"]})
        self.assertEqual(values, STACK_PARAMS)


class TestSsoDirectory(unittest.TestCase):
    class SsoAdmin:
        def list_instances(self):
            return {"Instances": [{"IdentityStoreId": "d-123"}]}

    class IdentityStore:
        def __init__(self, users):
            self.users, self.calls = users, 0
        def list_users(self, IdentityStoreId, Filters):
            assert IdentityStoreId == "d-123"
            assert Filters == [{"AttributePath": "UserName",
                                "AttributeValue": "sso@example.com"}]
            self.calls += 1
            return {"Users": self.users}

    def test_finds_a_user_and_caches_the_answer(self):
        store = self.IdentityStore([{"Name": {"GivenName": "AWS",
                                              "FamilyName": "Control Tower Admin"}}])
        directory = script.SsoDirectory(self.SsoAdmin(), store)
        self.assertEqual(directory.names("sso@example.com"), ("AWS", "Control Tower Admin"))
        self.assertEqual(directory.names("sso@example.com"), ("AWS", "Control Tower Admin"))
        self.assertEqual(store.calls, 1)  # second answer came from the cache

    def test_an_unknown_user_yields_empty_names(self):
        directory = script.SsoDirectory(self.SsoAdmin(), self.IdentityStore([]))
        self.assertEqual(directory.names("sso@example.com"), ("", ""))

    def test_no_identity_center_instance_yields_empty_names(self):
        class NoInstances:
            def list_instances(self):
                return {"Instances": []}

        directory = script.SsoDirectory(NoInstances(), self.IdentityStore([]))
        self.assertEqual(directory.names("sso@example.com"), ("", ""))


class TestInAnyOu(unittest.TestCase):
    def setUp(self):
        self.acct = account("sandbox-acct", ou_chain=list(NESTED_CHAIN))

    def test_matches_a_parent_ou_of_a_nested_account(self):
        self.assertTrue(script.in_any_ou(self.acct, ["Workloads"]))

    def test_matches_by_ou_id(self):
        self.assertTrue(script.in_any_ou(self.acct, ["ou-m3lh-6bhwnef5"]))

    def test_names_match_case_insensitively(self):
        self.assertTrue(script.in_any_ou(self.acct, ["workloads"]))

    def test_no_match(self):
        self.assertFalse(script.in_any_ou(self.acct, ["Security"]))


class TestSelectAccounts(unittest.TestCase):
    def setUp(self):
        self.sandbox = account("sandbox-acct", ou_chain=list(NESTED_CHAIN))
        self.production = account("prod-acct", ou_chain=[
            ("ou-m3lh-sz6b0e11", "Production"), ("ou-m3lh-6bhwnef5", "Workloads"), ("r-m3lh", "Root")])
        self.unresolved = account("mystery")  # OU lookup failed
        self.busy = account("busy", state="UNDER_CHANGE", ou_chain=list(NESTED_CHAIN))

    def select(self, accounts, args, keys=()):
        with redirect_stderr(io.StringIO()) as stderr:
            kept = script.select_accounts(accounts, args, list(keys))
        return kept, stderr.getvalue()

    def test_exclude_covers_accounts_in_nested_ous(self):
        kept, _ = self.select([self.sandbox, self.production], ou_args(exclude=["Workloads"]))
        self.assertEqual(kept, [])

    def test_include_keeps_only_matching_accounts(self):
        kept, _ = self.select([self.sandbox, self.production], ou_args(include=["Sandbox"]))
        self.assertEqual(kept, [self.sandbox])

    def test_exclude_wins_over_include(self):
        kept, _ = self.select([self.sandbox], ou_args(include=["Workloads"], exclude=["Sandbox"]))
        self.assertEqual(kept, [])

    def test_unknown_ou_is_skipped_when_filters_are_active(self):
        # A filter must never fail open: if we can't tell which OU an account is
        # in, it is not updated.
        kept, warnings = self.select([self.unresolved], ou_args(exclude=["Workloads"]))
        self.assertEqual(kept, [])
        self.assertIn("excluded to be safe", warnings)

    def test_unknown_ou_is_kept_when_no_filters_are_active(self):
        kept, _ = self.select([self.unresolved], ou_args())
        self.assertEqual(kept, [self.unresolved])

    def test_accounts_mid_change_are_skipped(self):
        kept, warnings = self.select([self.sandbox, self.busy], ou_args())
        self.assertEqual(kept, [self.sandbox])
        self.assertIn("UNDER_CHANGE", warnings)

    def test_exclude_account_matches_by_name(self):
        kept, warnings = self.select([self.sandbox, self.production],
                                     ou_args(exclude_account=["SANDBOX-ACCT"]))
        self.assertEqual(kept, [self.production])
        self.assertIn("--exclude-account", warnings)

    def test_exclude_account_matches_by_account_id(self):
        self.sandbox.account_id = "361769582329"
        kept, _ = self.select([self.sandbox, self.production],
                              ou_args(exclude_account=["361769582329"]))
        self.assertEqual(kept, [self.production])

    def test_accounts_with_unknown_parameter_values_are_skipped(self):
        # Updating without the current values could change the account's
        # configuration, so the script must never guess.
        complete = account("complete", parameters=dict(STACK_PARAMS))
        incomplete = account("incomplete", parameters={"AccountEmail": "a@example.com"})
        kept, warnings = self.select([complete, incomplete], ou_args(),
                                     keys=script.ACCOUNT_PARAM_KEYS)
        self.assertEqual(kept, [complete])
        self.assertIn("current value unknown", warnings)
        self.assertIn("SSOUserFirstName", warnings)


class TestStartUpdate(unittest.TestCase):
    def test_resends_the_current_value_of_every_parameter(self):
        # This is the core safety property: an update must re-send the exact
        # current values (Control Tower rejects UsePreviousValue) -- anything
        # else could change the account's email, SSO user or OU.
        class Catalog:
            def update_provisioned_product(self, **kwargs):
                assert kwargs["ProvisionedProductId"] == "pp-target"
                assert kwargs["ProductId"] == "prod-1"
                assert kwargs["ProvisioningArtifactId"] == "pa-new"
                assert kwargs["ProvisioningParameters"] == [
                    {"Key": key, "Value": STACK_PARAMS[key]}
                    for key in script.ACCOUNT_PARAM_KEYS]
                return {"RecordDetail": {"RecordId": "rec-1", "Status": "CREATED"}}

        acct = account("target", parameters=dict(STACK_PARAMS))
        script.start_update(Catalog(), acct, "prod-1", "pa-new", script.ACCOUNT_PARAM_KEYS)
        self.assertEqual(acct.record_id, "rec-1")
        self.assertEqual(acct.result, "CREATED")


class TestWaitForBatch(unittest.TestCase):
    class Catalog:
        def describe_record(self, Id):
            records = {
                "rec-ok": {"Status": "SUCCEEDED"},
                "rec-bad": {"Status": "FAILED", "RecordErrors": [{"Description": "boom"}]},
            }
            return {"RecordDetail": records[Id]}

    def wait(self, batch, timeout=5):
        with redirect_stdout(io.StringIO()):
            script.wait_for_batch(self.Catalog(), batch, poll_interval=0, timeout=timeout)

    def test_success_and_failure_are_reported_per_account(self):
        ok = account("ok", record_id="rec-ok")
        bad = account("bad", record_id="rec-bad")
        self.wait([ok, bad])
        self.assertEqual(ok.result, "SUCCEEDED")
        self.assertEqual(bad.result, "FAILED")
        self.assertEqual(bad.error, "boom")

    def test_updates_still_running_at_the_deadline_are_marked_timed_out(self):
        class SlowCatalog:
            def describe_record(self, Id):
                return {"RecordDetail": {"Status": "IN_PROGRESS"}}

        slow = account("slow", record_id="rec-slow")
        with redirect_stdout(io.StringIO()):
            script.wait_for_batch(SlowCatalog(), [slow], poll_interval=0, timeout=0)
        self.assertEqual(slow.result, "TIMED_OUT")

    def test_a_status_read_error_fails_that_account_only(self):
        class Catalog:
            def describe_record(self, Id):
                if Id == "rec-err":
                    raise access_denied("DescribeRecord")
                return {"RecordDetail": {"Status": "SUCCEEDED"}}

        err = account("err", record_id="rec-err")
        ok = account("ok", record_id="rec-ok")
        with redirect_stdout(io.StringIO()):
            script.wait_for_batch(Catalog(), [err, ok], poll_interval=0, timeout=5)
        self.assertEqual(err.result, "FAILED")
        self.assertEqual(ok.result, "SUCCEEDED")

    def test_accounts_that_never_started_are_not_polled(self):
        never_started = account("never")  # no record_id: submission failed earlier
        self.wait([never_started], timeout=0)
        self.assertIsNone(never_started.result)


class TestLandingZoneIsBusy(unittest.TestCase):
    class ControlTower:
        def __init__(self, operations):
            self.operations = operations
        def list_landing_zone_operations(self, filter):
            assert filter == {"statuses": ["IN_PROGRESS"]}
            return {"landingZoneOperations": self.operations}

    def test_busy_while_an_operation_is_in_progress(self):
        ct = self.ControlTower([{"operationType": "UPDATE", "status": "IN_PROGRESS"}])
        self.assertTrue(script.landing_zone_is_busy(ct))

    def test_not_busy_when_no_operations_are_running(self):
        self.assertFalse(script.landing_zone_is_busy(self.ControlTower([])))


class TestBatching(unittest.TestCase):
    def test_twelve_accounts_become_batches_of_five_five_two(self):
        accounts = [account(f"a{i}") for i in range(12)]
        batches = [accounts[i:i + 5] for i in range(0, len(accounts), 5)]
        self.assertEqual([len(b) for b in batches], [5, 5, 2])


if __name__ == "__main__":
    unittest.main()
