# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "boto3>=1.34",
# ]
# ///
"""Tests for reregister_control_tower_ous.py.

All AWS calls are stubbed, so the tests run offline and never touch an OU.

Run them with any of:
    uv run scripts/test_reregister_control_tower_ous.py
    python3 -m unittest discover -s scripts -v
    pytest scripts/
"""

import io
import json
import logging
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).parent))
import reregister_control_tower_ous as script  # noqa: E402

# Keep the script's own logging out of the test output. A real handler also
# stops logging's "last resort" handler from writing to stderr; assertLogs still
# sees every record.
script.log.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------- #
# Fixtures: a small organization mirroring a real Control Tower layout.
#
#   r-m3lh (Root)
#   |- ou-m3lh-secure01  Security    <- core OU (security + log archive)
#   |- ou-m3lh-6bhwnef5  Workloads
#   |  |- ou-m3lh-ddvurul0  Sandbox
#   |  \- ou-m3lh-sz6b0e11  Production
#   \- ou-m3lh-suspend1  Suspended   (not Control Tower managed)
# --------------------------------------------------------------------------- #
ROOT = "r-m3lh"
SECURITY = "ou-m3lh-secure01"
WORKLOADS = "ou-m3lh-6bhwnef5"
SANDBOX = "ou-m3lh-ddvurul0"
PRODUCTION = "ou-m3lh-sz6b0e11"
SUSPENDED = "ou-m3lh-suspend1"

MANAGEMENT_ACCOUNT = "999999999999"
SECURITY_ACCOUNT = "111111111111"
LOG_ARCHIVE_ACCOUNT = "222222222222"

OU_NAMES = {
    SECURITY: "Security",
    WORKLOADS: "Workloads",
    SANDBOX: "Sandbox",
    PRODUCTION: "Production",
    SUSPENDED: "Suspended",
}
OU_TREE = {
    ROOT: [SECURITY, WORKLOADS, SUSPENDED],
    WORKLOADS: [SANDBOX, PRODUCTION],
}
OU_ACCOUNTS = {
    SECURITY: [SECURITY_ACCOUNT, LOG_ARCHIVE_ACCOUNT],
    SANDBOX: ["333333333333"],
    PRODUCTION: ["444444444444", "555555555555"],
}

# Depth-first pre-order, parent before child.
EXPECTED_OU_ORDER = [SECURITY, WORKLOADS, SANDBOX, PRODUCTION, SUSPENDED]

# ARNs of the baseline *definitions*.
CT_BASELINE_ARN = "arn:aws:controltower:::baseline/17BSJV3IGJ2QSGA2"
IDENTITY_CENTER_BASELINE_ARN = "arn:aws:controltower:::baseline/LN25R72TTG6IGPTQ"


def ou_arn(ou_id: str) -> str:
    return f"arn:aws:organizations::{MANAGEMENT_ACCOUNT}:ou/o-abc1234567/{ou_id}"


def enabled_baseline(
    ou_id: str,
    version: str = "4.0",
    status: str = "SUCCEEDED",
    baseline: str = CT_BASELINE_ARN,
    arn: str | None = None,
) -> dict:
    """An entry as returned by ListEnabledBaselines."""
    return {
        "arn": arn if arn is not None else f"arn:aws:controltower:eu-west-1:1:enabledbaseline/{ou_id}",
        "baselineIdentifier": baseline,
        "baselineVersion": version,
        "targetIdentifier": ou_arn(ou_id),
        "statusSummary": {"status": status},
    }


def conflict(message: str = "another operation is in progress") -> ClientError:
    return ClientError({"Error": {"Code": "ConflictException", "Message": message}}, "ResetEnabledBaseline")


def access_denied(operation: str) -> ClientError:
    return ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, operation)


class FakeOrg:
    """AWS Organizations stub backed by the OU tree above."""

    def __init__(self, tree=None, accounts=None, names=None, management=MANAGEMENT_ACCOUNT):
        self.tree = OU_TREE if tree is None else tree
        self.accounts = OU_ACCOUNTS if accounts is None else accounts
        self.names = OU_NAMES if names is None else names
        self.management = management

    def describe_organization(self):
        return {"Organization": {"ManagementAccountId": self.management}}

    def list_roots(self):
        return {"Roots": [{"Id": ROOT, "Name": "Root"}]}

    def describe_organizational_unit(self, OrganizationalUnitId):
        if OrganizationalUnitId not in self.names:
            raise ClientError(
                {"Error": {"Code": "OrganizationalUnitNotFoundException", "Message": "no such OU"}},
                "DescribeOrganizationalUnit",
            )
        return {"OrganizationalUnit": {"Id": OrganizationalUnitId,
                                       "Name": self.names[OrganizationalUnitId]}}

    def get_paginator(self, name):
        org = self

        if name == "list_organizational_units_for_parent":
            class Paginator:
                def paginate(self, ParentId):
                    return [{"OrganizationalUnits": [
                        {"Id": ou_id, "Name": org.names.get(ou_id, "")}
                        for ou_id in org.tree.get(ParentId, [])]}]
            return Paginator()

        if name == "list_accounts_for_parent":
            class Paginator:
                def paginate(self, ParentId):
                    return [{"Accounts": [
                        {"Id": acct, "Name": f"acct-{acct[:4]}"}
                        for acct in org.accounts.get(ParentId, [])]}]
            return Paginator()

        raise AssertionError(f"unexpected paginator: {name}")


class FakeCT:
    """Control Tower stub. Reads are driven by the constructor arguments;
    writes are recorded and answered from `operations`."""

    def __init__(
        self,
        enabled=(),
        lz_version="3.3",
        manifest=None,
        baselines=(("AWSControlTowerBaseline", CT_BASELINE_ARN),),
        parameters=None,
        page_size=None,
    ):
        self.enabled = list(enabled)
        self.lz_version = lz_version
        self.manifest = {
            "securityRoles": {"accountId": SECURITY_ACCOUNT},
            "centralizedLogging": {"accountId": LOG_ARCHIVE_ACCOUNT},
        } if manifest is None else manifest
        self.baselines = list(baselines)
        self.parameters = parameters or {}
        self.page_size = page_size
        self.resets: list[str] = []
        self.updates: list[dict] = []
        self.operations: dict[str, list[str]] = {}
        self.reset_errors: list[Exception] = []

    # -- reads -- #
    def list_landing_zones(self):
        return {"landingZones": [{"arn": "arn:aws:controltower:eu-west-1:1:landingzone/LZ"}]}

    def get_landing_zone(self, landingZoneIdentifier):
        # A fresh manifest each call: landing_zone_manifest() mutates it.
        return {"landingZone": {"version": self.lz_version, "manifest": dict(self.manifest)}}

    def list_enabled_baselines(self, **kwargs):
        if self.page_size is None:
            return {"enabledBaselines": list(self.enabled)}
        start = int(kwargs.get("nextToken") or 0)
        page = self.enabled[start:start + self.page_size]
        nxt = start + self.page_size
        resp = {"enabledBaselines": page}
        if nxt < len(self.enabled):
            resp["nextToken"] = str(nxt)
        return resp

    def list_baselines(self, **kwargs):
        return {"baselines": [{"name": name, "arn": arn} for name, arn in self.baselines]}

    def get_enabled_baseline(self, enabledBaselineIdentifier):
        params = self.parameters.get(enabledBaselineIdentifier, [])
        return {"enabledBaselineDetails": {"parameters": params}}

    # -- writes -- #
    def reset_enabled_baseline(self, enabledBaselineIdentifier):
        if self.reset_errors:
            raise self.reset_errors.pop(0)
        self.resets.append(enabledBaselineIdentifier)
        op_id = f"op-reset-{len(self.resets)}"
        self.operations.setdefault(op_id, ["SUCCEEDED"])
        return {"operationIdentifier": op_id}

    def update_enabled_baseline(self, **kwargs):
        self.updates.append(kwargs)
        op_id = f"op-update-{len(self.updates)}"
        self.operations.setdefault(op_id, ["SUCCEEDED"])
        return {"operationIdentifier": op_id}

    def get_baseline_operation(self, operationIdentifier):
        statuses = self.operations.get(operationIdentifier) or ["SUCCEEDED"]
        status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
        return {"baselineOperation": {"status": status, "operationType": "RESET_ENABLED_BASELINE"}}


def plan_for(plans, ou_id) -> script.OUPlan:
    for p in plans:
        if p.ou_id == ou_id:
            return p
    raise AssertionError(f"no plan for {ou_id}: {[p.ou_id for p in plans]}")


def build_plan(org=None, ct=None, *, skip=(), target=None, completed=(),
               upgrade=False, version_override=None):
    return script.build_plan(
        org or FakeOrg(), ct if ct is not None else FakeCT(),
        skip=set(skip), target=target, completed=set(completed),
        upgrade=upgrade, version_override=version_override,
    )


def apply_args(state_file="", ignore_errors=False) -> Namespace:
    # state_file="" disables writing, which save_state() handles by returning early.
    return Namespace(conflict_wait=0, conflict_attempts=3, poll_interval=0,
                     poll_timeout=5, state_file=state_file, ignore_errors=ignore_errors)


class TestParseOuId(unittest.TestCase):
    def test_a_bare_ou_id_passes_through(self):
        self.assertEqual(script.parse_ou_id(SANDBOX), SANDBOX)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(script.parse_ou_id(f"  {SANDBOX}\n"), SANDBOX)

    def test_an_ou_arn_yields_the_bare_id(self):
        self.assertEqual(script.parse_ou_id(ou_arn(SANDBOX)), SANDBOX)

    def test_non_ou_targets_yield_none(self):
        # ListEnabledBaselines also returns account targets, which must be ignored.
        self.assertIsNone(script.parse_ou_id("arn:aws:organizations::1:account/o-abc1234567/333333333333"))
        self.assertIsNone(script.parse_ou_id(ROOT))
        self.assertIsNone(script.parse_ou_id("Sandbox"))
        self.assertIsNone(script.parse_ou_id(None))
        self.assertIsNone(script.parse_ou_id(""))


class TestParseSkip(unittest.TestCase):
    def test_reads_a_comma_separated_list_and_ignores_blanks(self):
        self.assertEqual(script.parse_skip(f"{SANDBOX}, ,{PRODUCTION},"), {SANDBOX, PRODUCTION})

    def test_no_value_means_nothing_is_skipped(self):
        self.assertEqual(script.parse_skip(None), set())
        self.assertEqual(script.parse_skip(""), set())

    def test_an_ou_name_is_a_usage_error(self):
        # --skip takes ids only; silently ignoring a name would act on an OU the
        # operator asked to leave alone.
        with self.assertRaises(SystemExit) as ctx:
            script.parse_skip("Sandbox")
        self.assertIn("Sandbox", str(ctx.exception))

    def test_a_root_id_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            script.parse_skip(ROOT)


class TestResolveVersionOverride(unittest.TestCase):
    def test_no_flag_means_the_built_in_table_is_used(self):
        self.assertIsNone(script.resolve_version_override(None, upgrade=True))
        self.assertIsNone(script.resolve_version_override("", upgrade=True))

    def test_the_override_is_normalized_when_upgrading(self):
        self.assertEqual(script.resolve_version_override(" 5.0 ", upgrade=True), "5.0")

    def test_without_upgrade_the_override_is_really_ignored(self):
        # It used to reach the planner anyway, which skipped in-sync OUs with a
        # reason quoting the override instead of the landing zone's own target.
        with self.assertLogs(script.log, "WARNING") as logs:
            self.assertIsNone(script.resolve_version_override("5.0", upgrade=False))
        self.assertIn("--baseline-version is ignored", "\n".join(logs.output))

    def test_an_unparsable_override_is_a_usage_error(self):
        # Falling back to the built-in table would upgrade to a version the
        # operator did not ask for, which an upgrade cannot be rolled back from.
        for bad in ("v5.0", "latest", "5.0-beta"):
            with self.subTest(baseline_version=bad):
                with self.assertRaises(SystemExit) as ctx:
                    script.resolve_version_override(bad, upgrade=True)
                self.assertIn(bad, str(ctx.exception))

    def test_an_in_sync_ou_is_still_reset_when_the_override_is_ignored(self):
        # Landing zone 3.3 wants baseline 4.0, which this OU already has.
        ct = FakeCT(lz_version="3.3", enabled=[enabled_baseline(SANDBOX, version="4.0")])
        with self.assertLogs(script.log, "WARNING"):
            override = script.resolve_version_override("5.0", upgrade=False)
        plan = plan_for(build_plan(ct=ct, version_override=override), SANDBOX)
        self.assertEqual(plan.action, "apply")
        self.assertEqual([b.kind for b in plan.baselines], ["reset"])


class TestVersionHelpers(unittest.TestCase):
    def test_versions_compare_numerically_not_lexically(self):
        self.assertLess(script.version_tuple("3.3"), script.version_tuple("4.0"))
        self.assertLess(script.version_tuple("2.9"), script.version_tuple("2.10"))

    def test_unparsable_versions_yield_an_empty_tuple(self):
        self.assertEqual(script.version_tuple("v4.0"), ())
        self.assertEqual(script.version_tuple(""), ())
        self.assertEqual(script.version_tuple(None), ())

    def test_normalize_strips_padding_and_junk(self):
        self.assertEqual(script.normalize_version(" 4.0 "), "4.0")
        self.assertEqual(script.normalize_version("4.0.0"), "4.0.0")
        self.assertEqual(script.normalize_version("latest"), "")

    def test_each_landing_zone_range_maps_to_its_baseline_version(self):
        # https://docs.aws.amazon.com/controltower/latest/userguide/table-of-baselines.html
        for lz, expected in [
            ("2.0", "1.0"), ("2.7", "1.0"),
            ("2.8", "2.0"), ("2.9", "2.0"),
            ("3.0", "3.0"), ("3.1", "3.0"),
            ("3.2", "4.0"), ("3.3", "4.0"),
            ("4.0", "5.0"),
        ]:
            with self.subTest(landing_zone=lz):
                self.assertEqual(script.target_baseline_version_for_lz(lz), expected)

    def test_a_version_outside_the_table_yields_none(self):
        # Unknown landing zone: the caller falls back to a plain reset rather
        # than guessing an upgrade target.
        self.assertIsNone(script.target_baseline_version_for_lz("5.0"))
        self.assertIsNone(script.target_baseline_version_for_lz("1.0"))
        self.assertIsNone(script.target_baseline_version_for_lz(""))


class TestSummaryReaders(unittest.TestCase):
    def test_status_is_read_from_either_shape_and_upper_cased(self):
        self.assertEqual(script.baseline_status({"statusSummary": {"status": "succeeded"}}), "SUCCEEDED")
        self.assertEqual(script.baseline_status({"status": "under_change"}), "UNDER_CHANGE")
        self.assertEqual(script.baseline_status({}), "")

    def test_ids_and_versions_are_read_and_trimmed(self):
        summary = enabled_baseline(SANDBOX, version=" 4.0 ")
        self.assertEqual(script.enabled_baseline_id(summary),
                         f"arn:aws:controltower:eu-west-1:1:enabledbaseline/{SANDBOX}")
        self.assertEqual(script.enabled_baseline_version(summary), "4.0")
        self.assertEqual(script.baseline_identifier(summary), CT_BASELINE_ARN)

    def test_missing_fields_yield_empty_strings(self):
        self.assertEqual(script.enabled_baseline_id({}), "")
        self.assertEqual(script.enabled_baseline_version({}), "")
        self.assertEqual(script.baseline_identifier({}), "")


class TestIsConflictInProgress(unittest.TestCase):
    def test_recognises_the_retryable_conflict(self):
        self.assertTrue(script.is_conflict_in_progress(conflict()))
        self.assertTrue(script.is_conflict_in_progress(conflict("Another Operation Is In Progress")))

    def test_other_conflicts_are_not_retryable(self):
        self.assertFalse(script.is_conflict_in_progress(conflict("baseline is not enabled")))

    def test_other_error_codes_are_not_retryable(self):
        self.assertFalse(script.is_conflict_in_progress(access_denied("ResetEnabledBaseline")))


class TestOrganizationReads(unittest.TestCase):
    def test_ous_are_returned_top_down_parent_before_child(self):
        # Order matters: a child OU must not be reset before its parent.
        self.assertEqual(script.ordered_ou_ids(FakeOrg()), EXPECTED_OU_ORDER)

    def test_direct_accounts_exclude_accounts_in_child_ous(self):
        self.assertEqual(script.direct_account_ids(FakeOrg(), WORKLOADS), set())
        self.assertEqual(script.direct_account_ids(FakeOrg(), SANDBOX), {"333333333333"})

    def test_ou_name_lookup_failures_are_not_fatal(self):
        self.assertEqual(script.ou_name(FakeOrg(), "ou-m3lh-goneaway"), "")

    def test_management_account_id_is_read_from_the_organization(self):
        self.assertEqual(script.management_account_id(FakeOrg()), MANAGEMENT_ACCOUNT)


class TestLandingZoneManifest(unittest.TestCase):
    def test_the_version_is_attached_to_the_manifest(self):
        manifest = script.landing_zone_manifest(FakeCT(lz_version="3.3"))
        self.assertEqual(manifest["_version"], "3.3")
        self.assertEqual(script.landing_zone_version(FakeCT(lz_version=" 3.3 ")), "3.3")

    def test_a_json_encoded_manifest_is_parsed(self):
        class StringManifest(FakeCT):
            def get_landing_zone(self, landingZoneIdentifier):
                return {"landingZone": {"version": "4.0", "manifest": json.dumps(self.manifest)}}

        manifest = script.landing_zone_manifest(StringManifest())
        self.assertEqual(manifest["securityRoles"]["accountId"], SECURITY_ACCOUNT)

    def test_unreadable_manifests_degrade_to_empty(self):
        class Broken(FakeCT):
            def list_landing_zones(self):
                raise access_denied("ListLandingZones")

        class NoLandingZone(FakeCT):
            def list_landing_zones(self):
                return {"landingZones": []}

        class BadJson(FakeCT):
            def get_landing_zone(self, landingZoneIdentifier):
                return {"landingZone": {"version": "4.0", "manifest": "not json"}}

        self.assertEqual(script.landing_zone_manifest(Broken()), {})
        self.assertEqual(script.landing_zone_manifest(NoLandingZone()), {})
        self.assertEqual(script.landing_zone_manifest(BadJson()), {"_version": "4.0"})


class TestDetectCoreOu(unittest.TestCase):
    def test_finds_the_ou_holding_the_security_and_log_archive_accounts(self):
        self.assertEqual(script.detect_core_ou(FakeOrg(), FakeCT()), SECURITY)

    def test_the_management_account_is_not_used_as_a_marker(self):
        # On some landing zones the manifest names the management account as the
        # security account; matching on it would identify the wrong OU.
        ct = FakeCT(manifest={
            "securityRoles": {"accountId": MANAGEMENT_ACCOUNT},
            "centralizedLogging": {"accountId": LOG_ARCHIVE_ACCOUNT},
        })
        self.assertEqual(script.detect_core_ou(FakeOrg(), ct), SECURITY)

    def test_returns_none_when_the_manifest_has_no_core_accounts(self):
        with self.assertLogs(script.log, "WARNING") as logs:
            self.assertIsNone(script.detect_core_ou(FakeOrg(), FakeCT(manifest={})))
        self.assertIn("manifest", "\n".join(logs.output))

    def test_a_manifest_with_null_account_ids_is_not_fatal(self):
        # .get("accountId", "") returns None when the key is present but null,
        # so .strip() used to raise AttributeError and abort build_plan().
        ct = FakeCT(manifest={"securityRoles": {"accountId": None},
                              "centralizedLogging": {}})
        with self.assertLogs(script.log, "WARNING"):
            self.assertIsNone(script.detect_core_ou(FakeOrg(), ct))

    def test_returns_none_when_no_ou_holds_both_accounts(self):
        org = FakeOrg(accounts={SECURITY: [SECURITY_ACCOUNT], SANDBOX: [LOG_ARCHIVE_ACCOUNT]})
        with self.assertLogs(script.log, "WARNING"):
            self.assertIsNone(script.detect_core_ou(org, FakeCT()))


class TestEnabledBaselineInventory(unittest.TestCase):
    def test_baselines_are_grouped_by_ou_across_pages(self):
        ct = FakeCT(enabled=[enabled_baseline(SANDBOX), enabled_baseline(PRODUCTION),
                             enabled_baseline(SANDBOX, baseline=IDENTITY_CENTER_BASELINE_ARN,
                                              arn="arn:aws:controltower:eu-west-1:1:enabledbaseline/idc")],
                    page_size=1)
        inventory = script.enabled_baselines_by_ou(ct)
        self.assertEqual(sorted(inventory), sorted([SANDBOX, PRODUCTION]))
        self.assertEqual(len(inventory[SANDBOX]), 2)

    def test_non_ou_targets_are_ignored(self):
        account_target = dict(enabled_baseline(SANDBOX),
                              targetIdentifier="arn:aws:organizations::1:account/o-abc1234567/333333333333")
        self.assertEqual(script.enabled_baselines_by_ou(FakeCT(enabled=[account_target])), {})


class TestBaselineIdentification(unittest.TestCase):
    def test_the_control_tower_baseline_arn_is_looked_up_by_name(self):
        ct = FakeCT(baselines=[("IdentityCenterBaseline", IDENTITY_CENTER_BASELINE_ARN),
                               ("AWSControlTowerBaseline", CT_BASELINE_ARN)])
        self.assertEqual(script.aws_controltower_baseline_arn(ct), CT_BASELINE_ARN)

    def test_a_missing_baseline_definition_yields_none(self):
        self.assertEqual(script.aws_controltower_baseline_arn(FakeCT(baselines=[])), None)

    def test_baselines_are_matched_on_the_definition_arn(self):
        self.assertTrue(script.is_ct_baseline(enabled_baseline(SANDBOX), CT_BASELINE_ARN))
        self.assertFalse(script.is_ct_baseline(
            enabled_baseline(SANDBOX, baseline=IDENTITY_CENTER_BASELINE_ARN), CT_BASELINE_ARN))

    def test_the_name_fallback_is_used_when_the_arn_is_unknown(self):
        named = enabled_baseline(SANDBOX, baseline="arn:aws:controltower:::baseline/AWSControlTowerBaseline")
        self.assertTrue(script.is_ct_baseline(named, None))
        self.assertFalse(script.is_ct_baseline(enabled_baseline(SANDBOX, baseline="other"), None))


class TestEnabledBaselineParameters(unittest.TestCase):
    def test_existing_parameters_are_read_back_for_reuse_on_an_update(self):
        ct = FakeCT(parameters={"eb-1": [{"key": "IdentityCenterEnabledForThisOU", "value": "true"}]})
        self.assertEqual(script.enabled_baseline_parameters(ct, "eb-1"),
                         [{"key": "IdentityCenterEnabledForThisOU", "value": "true"}])

    def test_blank_keys_and_values_are_dropped(self):
        ct = FakeCT(parameters={"eb-1": [
            {"key": "Good", "value": "yes"},
            {"key": "", "value": "no key"},
            {"key": "NoValue", "value": None},
            {"key": "EmptyValue", "value": ""},
        ]})
        self.assertEqual(script.enabled_baseline_parameters(ct, "eb-1"), [{"key": "Good", "value": "yes"}])

    def test_a_read_failure_yields_no_parameters(self):
        class Broken(FakeCT):
            def get_enabled_baseline(self, enabledBaselineIdentifier):
                raise access_denied("GetEnabledBaseline")

        with self.assertLogs(script.log, "WARNING"):
            self.assertEqual(script.enabled_baseline_parameters(Broken(), "eb-1"), [])


class TestResolveTarget(unittest.TestCase):
    def test_an_ou_id_is_accepted_directly(self):
        self.assertEqual(script.resolve_target(FakeOrg(), SANDBOX), SANDBOX)

    def test_an_ou_name_is_resolved_case_insensitively(self):
        self.assertEqual(script.resolve_target(FakeOrg(), "sandbox"), SANDBOX)

    def test_an_unknown_name_is_a_usage_error(self):
        with self.assertRaises(SystemExit) as ctx:
            script.resolve_target(FakeOrg(), "Nope")
        self.assertIn("no OU found", str(ctx.exception))

    def test_an_ambiguous_name_asks_for_the_id_instead_of_guessing(self):
        names = dict(OU_NAMES, **{PRODUCTION: "Sandbox"})
        with self.assertRaises(SystemExit) as ctx:
            script.resolve_target(FakeOrg(names=names), "Sandbox")
        self.assertIn("multiple OUs", str(ctx.exception))
        self.assertIn(PRODUCTION, str(ctx.exception))


class TestBuildPlanSkips(unittest.TestCase):
    def setUp(self):
        self.ct = FakeCT(enabled=[enabled_baseline(ou) for ou in (SECURITY, WORKLOADS, SANDBOX, PRODUCTION)])

    def test_every_ou_is_reported_in_top_down_order(self):
        plans = build_plan(ct=self.ct)
        self.assertEqual([p.ou_id for p in plans], EXPECTED_OU_ORDER)

    def test_the_core_ou_is_always_skipped(self):
        self.assertEqual(plan_for(build_plan(ct=self.ct), SECURITY).action, "skip")
        self.assertIn("core", plan_for(build_plan(ct=self.ct), SECURITY).reason)

    def test_the_core_ou_is_skipped_even_when_explicitly_targeted(self):
        plans = build_plan(ct=self.ct, target=SECURITY)
        self.assertEqual([p.action for p in plans], ["skip"])

    def test_skipped_ous_are_not_acted_on(self):
        plan = plan_for(build_plan(ct=self.ct, skip=[SANDBOX]), SANDBOX)
        self.assertEqual((plan.action, plan.reason), ("skip", "in --skip list"))

    def test_ous_recorded_as_completed_are_skipped(self):
        plan = plan_for(build_plan(ct=self.ct, completed=[SANDBOX]), SANDBOX)
        self.assertEqual(plan.action, "skip")
        self.assertIn("already-completed", plan.reason)

    def test_target_mode_ignores_the_completed_list(self):
        # Re-running a single OU on purpose must not be silently skipped.
        plans = build_plan(ct=self.ct, target=SANDBOX, completed=[SANDBOX])
        self.assertEqual([(p.ou_id, p.action) for p in plans], [(SANDBOX, "apply")])

    def test_ous_without_an_enabled_baseline_are_not_managed(self):
        plan = plan_for(build_plan(ct=self.ct), SUSPENDED)
        self.assertEqual((plan.action, plan.reason), ("skip", "not Control Tower managed"))

    def test_an_ou_mid_operation_is_left_alone(self):
        ct = FakeCT(enabled=[enabled_baseline(SANDBOX, status="UNDER_CHANGE")])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual((plan.action, plan.reason), ("skip", "baseline under change"))

    def test_an_ou_whose_baseline_has_no_id_is_skipped(self):
        ct = FakeCT(enabled=[enabled_baseline(SANDBOX, arn="")])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual((plan.action, plan.reason), ("skip", "no resettable baseline id"))

    def test_an_unknown_target_produces_an_empty_plan(self):
        with self.assertLogs(script.log, "WARNING"):
            plans = build_plan(ct=self.ct, target="ou-m3lh-goneaway")
        self.assertEqual(plans, [])


class TestBuildPlanActions(unittest.TestCase):
    def test_a_baseline_already_at_the_right_version_is_reset(self):
        # Landing zone 3.3 wants baseline 4.0.
        ct = FakeCT(lz_version="3.3", enabled=[enabled_baseline(SANDBOX, version="4.0")])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual(plan.action, "apply")
        self.assertEqual([(b.kind, b.current_version, b.target_version) for b in plan.baselines],
                         [("reset", "4.0", "4.0")])

    def test_the_accounts_in_the_ou_are_recorded_for_the_operator(self):
        ct = FakeCT(enabled=[enabled_baseline(PRODUCTION)])
        self.assertEqual(plan_for(build_plan(ct=ct), PRODUCTION).accounts,
                         ["444444444444", "555555555555"])

    def test_an_outdated_baseline_is_refused_without_upgrade(self):
        ct = FakeCT(lz_version="3.3", enabled=[enabled_baseline(SANDBOX, version="3.0")])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual(plan.action, "skip")
        self.assertIn("--upgrade", plan.reason)
        self.assertIn("v4.0", plan.reason)

    def test_upgrade_turns_an_outdated_baseline_into_an_update(self):
        eb = f"arn:aws:controltower:eu-west-1:1:enabledbaseline/{SANDBOX}"
        ct = FakeCT(lz_version="3.3", enabled=[enabled_baseline(SANDBOX, version="3.0")],
                    parameters={eb: [{"key": "IdentityCenterEnabledForThisOU", "value": "true"}]})
        plan = plan_for(build_plan(ct=ct, upgrade=True), SANDBOX)
        self.assertEqual(plan.action, "apply")
        action = plan.baselines[0]
        self.assertEqual((action.kind, action.current_version, action.target_version),
                         ("update", "3.0", "4.0"))
        # Existing parameters must be carried over, or the update would drop them.
        self.assertEqual(action.parameters, [{"key": "IdentityCenterEnabledForThisOU", "value": "true"}])

    def test_a_version_override_replaces_the_built_in_table(self):
        ct = FakeCT(lz_version="3.3", enabled=[enabled_baseline(SANDBOX, version="4.0")])
        plan = plan_for(build_plan(ct=ct, upgrade=True, version_override="5.0"), SANDBOX)
        action = plan.baselines[0]
        self.assertEqual((action.kind, action.target_version), ("update", "5.0"))

    def test_an_unknown_landing_zone_version_falls_back_to_a_plain_reset(self):
        ct = FakeCT(lz_version="9.9", enabled=[enabled_baseline(SANDBOX, version="4.0")])
        with self.assertLogs(script.log, "WARNING") as logs:
            plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual(plan.action, "apply")
        self.assertEqual(plan.baselines[0].kind, "reset")
        self.assertIn("--baseline-version", "\n".join(logs.output))

    def test_non_control_tower_baselines_are_only_ever_reset(self):
        # The version table only applies to AWSControlTowerBaseline; other
        # baselines (e.g. Identity Center) must not be version-checked.
        ct = FakeCT(lz_version="3.3", enabled=[
            enabled_baseline(SANDBOX, version="1.0", baseline=IDENTITY_CENTER_BASELINE_ARN)])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual(plan.action, "apply")
        self.assertEqual([b.kind for b in plan.baselines], ["reset"])

    def test_all_baselines_on_an_ou_are_planned(self):
        ct = FakeCT(enabled=[
            enabled_baseline(SANDBOX),
            enabled_baseline(SANDBOX, baseline=IDENTITY_CENTER_BASELINE_ARN,
                             arn="arn:aws:controltower:eu-west-1:1:enabledbaseline/idc"),
        ])
        self.assertEqual(len(plan_for(build_plan(ct=ct), SANDBOX).baselines), 2)

    def test_one_blocked_baseline_blocks_the_whole_ou(self):
        # Partially re-registering an OU would leave it in a mixed state.
        ct = FakeCT(lz_version="3.3", enabled=[
            enabled_baseline(SANDBOX, version="4.0"),
            enabled_baseline(SANDBOX, version="3.0",
                             arn="arn:aws:controltower:eu-west-1:1:enabledbaseline/old"),
        ])
        plan = plan_for(build_plan(ct=ct), SANDBOX)
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.baselines, [])


class TestPrintPlan(unittest.TestCase):
    def render(self, plans) -> str:
        out = io.StringIO()
        with redirect_stdout(out):
            script.print_plan(plans)
        return out.getvalue()

    def test_counts_and_reasons_are_shown(self):
        plans = [
            script.OUPlan(SANDBOX, "Sandbox", "apply", "managed",
                          [script.BaselineAction("eb-1", "reset", "4.0", "4.0")], ["333333333333"]),
            script.OUPlan(SECURITY, "Security", "skip", "core/security OU"),
        ]
        text = self.render(plans)
        self.assertIn("to act: 1", text)
        self.assertIn("to skip: 1", text)
        self.assertIn("reset v4.0", text)
        self.assertIn("core/security OU", text)

    def test_upgrades_are_called_out_separately(self):
        plans = [script.OUPlan(SANDBOX, "Sandbox", "apply", "managed",
                               [script.BaselineAction("eb-1", "update", "3.0", "4.0")])]
        text = self.render(plans)
        self.assertIn("baseline upgrades: 1", text)
        self.assertIn("UPDATE v3.0->v4.0", text)

    def test_an_empty_plan_says_so(self):
        self.assertIn("Will ACT: (nothing)", self.render([]))


class TestStartWithConflictWait(unittest.TestCase):
    def test_a_reset_sends_only_the_enabled_baseline_id(self):
        ct = FakeCT()
        self.assertEqual(script.reset_baseline(ct, "eb-1", 0, 3), "op-reset-1")
        self.assertEqual(ct.resets, ["eb-1"])

    def test_an_update_sends_the_target_version_and_parameters(self):
        ct = FakeCT()
        params = [{"key": "IdentityCenterEnabledForThisOU", "value": "true"}]
        script.update_baseline(ct, "eb-1", "4.0", params, 0, 3)
        self.assertEqual(ct.updates, [{"enabledBaselineIdentifier": "eb-1",
                                       "baselineVersion": "4.0", "parameters": params}])

    def test_an_update_omits_the_parameters_key_when_there_are_none(self):
        # Sending an empty list would clear the baseline's parameters.
        ct = FakeCT()
        script.update_baseline(ct, "eb-1", "4.0", [], 0, 3)
        self.assertNotIn("parameters", ct.updates[0])

    def test_a_busy_baseline_is_retried_until_it_frees_up(self):
        ct = FakeCT()
        ct.reset_errors = [conflict(), conflict()]
        with self.assertLogs(script.log, "INFO"):
            self.assertEqual(script.reset_baseline(ct, "eb-1", 0, 5), "op-reset-1")
        self.assertEqual(ct.resets, ["eb-1"])

    def test_a_permanently_busy_baseline_gives_up_with_a_clear_error(self):
        ct = FakeCT()
        ct.reset_errors = [conflict() for _ in range(5)]
        with self.assertLogs(script.log, "INFO"), self.assertRaises(script.OperationFailed) as err:
            script.reset_baseline(ct, "eb-1", 0, 3)
        self.assertIn("still blocked after 3 attempts", str(err.exception))

    def test_other_errors_are_not_retried(self):
        ct = FakeCT()
        ct.reset_errors = [access_denied("ResetEnabledBaseline"), conflict()]
        with self.assertRaises(ClientError):
            script.reset_baseline(ct, "eb-1", 0, 3)


class TestWaitForOperation(unittest.TestCase):
    class CT:
        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.calls = 0

        def get_baseline_operation(self, operationIdentifier):
            self.calls += 1
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return {"baselineOperation": {"status": status, "statusMessage": "boom",
                                          "operationType": "RESET_ENABLED_BASELINE"}}

    def test_polling_continues_until_the_operation_succeeds(self):
        ct = self.CT(["IN_PROGRESS", "IN_PROGRESS", "SUCCEEDED"])
        op = script.wait_for_operation(ct, "op-1", poll_interval=0, timeout=30)
        self.assertEqual(op["status"], "SUCCEEDED")
        self.assertEqual(ct.calls, 3)

    def test_a_failed_operation_raises_with_the_service_message(self):
        with self.assertRaises(script.OperationFailed) as err:
            script.wait_for_operation(self.CT(["FAILED"]), "op-1", poll_interval=0, timeout=30)
        self.assertIn("boom", str(err.exception))

    def test_an_operation_that_never_finishes_times_out(self):
        with self.assertRaises(script.OperationFailed) as err:
            script.wait_for_operation(self.CT(["IN_PROGRESS"]), "op-1", poll_interval=0, timeout=0)
        self.assertIn("timed out", str(err.exception))
        self.assertIn("IN_PROGRESS", str(err.exception))


class TestStateFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "state.json")

    def test_progress_survives_a_round_trip(self):
        script.save_state(self.path, {"completed": [SANDBOX], "failed": {PRODUCTION: "boom"}})
        state = script.load_state(self.path)
        self.assertEqual(state["completed"], [SANDBOX])
        self.assertEqual(state["failed"], {PRODUCTION: "boom"})

    def test_a_missing_file_starts_from_scratch(self):
        self.assertEqual(script.load_state(self.path), {"completed": [], "failed": {}})
        self.assertEqual(script.load_state(""), {"completed": [], "failed": {}})

    def test_a_corrupt_file_starts_from_scratch_with_a_warning(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        with self.assertLogs(script.log, "WARNING"):
            self.assertEqual(script.load_state(self.path), {"completed": [], "failed": {}})

    def test_missing_keys_are_filled_in(self):
        Path(self.path).write_text("{}", encoding="utf-8")
        self.assertEqual(script.load_state(self.path), {"completed": [], "failed": {}})

    def test_an_unwritable_path_warns_instead_of_crashing(self):
        # Losing the progress file must not abort a run that is already underway.
        with self.assertLogs(script.log, "WARNING"):
            script.save_state(os.path.join(self.tmp.name, "nope", "state.json"), {"completed": []})

    def test_no_path_means_no_file(self):
        script.save_state("", {"completed": [SANDBOX]})  # must not raise
        self.assertEqual(os.listdir(self.tmp.name), [])


class TestApplyPlan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = os.path.join(self.tmp.name, "state.json")

    @staticmethod
    def ou_plan(ou_id, name, *baselines, accounts=()):
        return script.OUPlan(ou_id, name, "apply", "managed", list(baselines), list(accounts))

    def run_apply(self, ct, plans, args=None, state=None):
        args = args or apply_args()
        state = state if state is not None else {"completed": [], "failed": {}}
        with redirect_stdout(io.StringIO()) as out:
            failures = script.apply_plan(ct, plans, args, state)
        return failures, state, out.getvalue()

    def test_each_planned_baseline_is_acted_on_and_recorded(self):
        ct = FakeCT()
        plans = [
            self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-sandbox", "reset", "4.0", "4.0"),
                         accounts=["333333333333"]),
            self.ou_plan(PRODUCTION, "Production",
                         script.BaselineAction("eb-prod", "update", "3.0", "4.0",
                                               [{"key": "K", "value": "v"}])),
        ]
        failures, state, out = self.run_apply(ct, plans)
        self.assertEqual(failures, 0)
        self.assertEqual(ct.resets, ["eb-sandbox"])
        self.assertEqual(ct.updates[0]["baselineVersion"], "4.0")
        self.assertEqual(state["completed"], sorted([SANDBOX, PRODUCTION]))
        self.assertIn("SUCCEEDED", out)

    def test_ous_marked_skip_are_not_touched(self):
        ct = FakeCT()
        plans = [script.OUPlan(SECURITY, "Security", "skip", "core/security OU")]
        failures, state, _ = self.run_apply(ct, plans)
        self.assertEqual((failures, ct.resets, ct.updates), (0, [], []))
        self.assertEqual(state["completed"], [])

    def test_progress_is_written_after_every_ou(self):
        # An interrupted run must be able to resume from the last finished OU.
        ct = FakeCT()
        plans = [self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-1", "reset", "4.0", "4.0"))]
        self.run_apply(ct, plans, args=apply_args(state_file=self.state_path))
        self.assertEqual(script.load_state(self.state_path)["completed"], [SANDBOX])

    def test_a_failure_stops_the_run_by_default(self):
        ct = FakeCT()
        ct.reset_errors = [access_denied("ResetEnabledBaseline")]
        plans = [
            self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-1", "reset", "4.0", "4.0")),
            self.ou_plan(PRODUCTION, "Production", script.BaselineAction("eb-2", "reset", "4.0", "4.0")),
        ]
        with self.assertLogs(script.log, "ERROR"):
            failures, state, out = self.run_apply(ct, plans, args=apply_args(state_file=self.state_path))
        self.assertEqual(failures, 1)
        self.assertEqual(ct.resets, [])  # the second OU was never started
        self.assertIn("Stopping", out)
        self.assertIn(SANDBOX, script.load_state(self.state_path)["failed"])

    def test_ignore_errors_continues_with_the_remaining_ous(self):
        ct = FakeCT()
        ct.reset_errors = [access_denied("ResetEnabledBaseline")]
        plans = [
            self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-1", "reset", "4.0", "4.0")),
            self.ou_plan(PRODUCTION, "Production", script.BaselineAction("eb-2", "reset", "4.0", "4.0")),
        ]
        with self.assertLogs(script.log, "ERROR"):
            failures, state, _ = self.run_apply(ct, plans, args=apply_args(ignore_errors=True))
        self.assertEqual(failures, 1)
        self.assertEqual(ct.resets, ["eb-2"])
        self.assertEqual(state["completed"], [PRODUCTION])
        self.assertIn(SANDBOX, state["failed"])

    def test_a_failed_operation_is_recorded_as_a_failure(self):
        ct = FakeCT()
        ct.operations["op-reset-1"] = ["FAILED"]
        plans = [self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-1", "reset", "4.0", "4.0"))]
        with self.assertLogs(script.log, "ERROR"):
            failures, state, _ = self.run_apply(ct, plans)
        self.assertEqual(failures, 1)
        self.assertEqual(state["completed"], [])
        self.assertIn(SANDBOX, state["failed"])

    def test_a_retried_ou_clears_its_earlier_failure(self):
        ct = FakeCT()
        state = {"completed": [], "failed": {SANDBOX: "boom"}}
        plans = [self.ou_plan(SANDBOX, "Sandbox", script.BaselineAction("eb-1", "reset", "4.0", "4.0"))]
        self.run_apply(ct, plans, state=state)
        self.assertEqual(state["failed"], {})
        self.assertEqual(state["completed"], [SANDBOX])

    def test_an_ou_is_only_completed_once_all_its_baselines_succeed(self):
        ct = FakeCT()
        ct.operations["op-reset-2"] = ["FAILED"]
        plans = [self.ou_plan(SANDBOX, "Sandbox",
                              script.BaselineAction("eb-1", "reset", "4.0", "4.0"),
                              script.BaselineAction("eb-2", "reset", "4.0", "4.0"))]
        with self.assertLogs(script.log, "ERROR"):
            failures, state, _ = self.run_apply(ct, plans)
        self.assertEqual(failures, 1)
        self.assertEqual(ct.resets, ["eb-1", "eb-2"])
        self.assertEqual(state["completed"], [])


class TestBuildParser(unittest.TestCase):
    def test_defaults_are_a_resumable_dry_run(self):
        args = script.build_parser().parse_args([])
        self.assertFalse(args.apply)
        self.assertFalse(args.yes)
        self.assertFalse(args.upgrade)
        self.assertFalse(args.ignore_errors)
        self.assertFalse(args.restart)
        self.assertEqual(args.state_file, "ct_reregister_state.json")
        self.assertEqual(args.poll_interval, 30)
        self.assertEqual(args.poll_timeout, 7200)
        self.assertEqual(args.conflict_wait, 30)
        self.assertEqual(args.conflict_attempts, 20)

    def test_all_flags_are_parsed(self):
        args = script.build_parser().parse_args([
            "--region", "eu-west-1", "--profile", "mgmt", "--target", "Sandbox",
            "--skip", SANDBOX, "--upgrade", "--baseline-version", "5.0",
            "--apply", "--yes", "--ignore-errors", "--state-file", "/tmp/s.json",
            "--restart", "--poll-interval", "5", "--poll-timeout", "60",
            "--conflict-wait", "1", "--conflict-attempts", "2", "-v",
        ])
        self.assertEqual((args.region, args.profile, args.target), ("eu-west-1", "mgmt", "Sandbox"))
        self.assertEqual(args.skip, SANDBOX)
        self.assertEqual(args.baseline_version, "5.0")
        self.assertTrue(args.upgrade and args.apply and args.yes)
        self.assertTrue(args.ignore_errors and args.restart and args.verbose)
        self.assertEqual(args.state_file, "/tmp/s.json")


if __name__ == "__main__":
    unittest.main()
