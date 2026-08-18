#!/usr/bin/env python3
"""
ct_reregister_ous.py

Sequentially reset the enabled AWS Control Tower baseline for every managed OU in
the organization ("re-registration"), one OU at a time, run from your laptop.

This is a lean local port of the aws-samples "ControlTower Organization
ReRegistration" CloudFormation automation. Instead of EventBridge + Step Functions
+ Organizations tags, it drives the whole thing synchronously:

  * walks Control Tower managed OUs top-down
  * skips ROOT, the Control Tower core/security OU, and any --skip OUs
  * resets each OU's enabled baseline(s) and polls to completion before the next OU
  * resumable via a local JSON state file
  * DRY-RUN by default; pass --apply to actually reset

It resets the enabled baselines that already exist on each managed OU. With
--upgrade it will additionally UpdateEnabledBaseline for OUs whose baseline
version is behind the landing zone (a reset alone fails on those). It does NOT
do the optional-controls reset the CloudFormation template does.

Run it with credentials for the Control Tower MANAGEMENT account, in the Control
Tower home region, with permissions roughly equivalent to the engine role in the
template (controltower:*Baseline*, organizations:List*/Describe*, etc.).

Examples
--------
  # See the plan, change nothing:
  python ct_reregister_ous.py --region eu-west-1 --profile my-mgmt-sso

  # Actually reset everything, skipping two OUs:
  python ct_reregister_ous.py --region eu-west-1 --apply --skip ou-ab12-11111111,ou-ab12-22222222

  # Reset just one OU (test first!) - by id or by name:
  python ct_reregister_ous.py --region eu-west-1 --apply --target ou-ab12-33333333
  python ct_reregister_ous.py --region eu-west-1 --apply --target "Sandbox"
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterable

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("ct-reregister")

OU_ID_RE = re.compile(r"^ou-[a-z0-9]{4,32}-[a-z0-9]{8,32}$")
OU_ARN_RE = re.compile(r":ou/o-[a-z0-9]{10,32}/(ou-[a-z0-9]{4,32}-[a-z0-9]{8,32})$")

# Baseline is mid-operation; not eligible to start a new reset.
BUSY_BASELINE_STATUSES = {"UNDER_CHANGE", "PLAN_IN_PROGRESS"}

BOTO_CFG = Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
    connect_timeout=10,
    read_timeout=120,
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def parse_ou_id(value: str | None) -> str | None:
    """Accept a bare OU id or an OU ARN and return the bare OU id."""
    if not value:
        return None
    value = str(value).strip()
    if OU_ID_RE.match(value):
        return value
    m = OU_ARN_RE.search(value)
    return m.group(1) if m else None


def baseline_status(summary: dict) -> str:
    status = (
        (summary.get("statusSummary") or {}).get("status")
        or summary.get("status")
        or ""
    )
    return str(status).upper()


def enabled_baseline_id(summary: dict) -> str:
    return (summary.get("arn") or "").strip()


def enabled_baseline_version(summary: dict) -> str:
    return str(summary.get("baselineVersion") or "").strip()


def baseline_identifier(summary: dict) -> str:
    """ARN of the baseline *definition* (not the enabled instance)."""
    return str(summary.get("baselineIdentifier") or "").strip()


def is_conflict_in_progress(err: ClientError) -> bool:
    e = err.response.get("Error") or {}
    return e.get("Code") == "ConflictException" and (
        "another operation is in progress" in str(e.get("Message", "")).lower()
    )


# --------------------------------------------------------------------------- #
# Landing-zone <-> AWSControlTowerBaseline version compatibility.
# Source: https://docs.aws.amazon.com/controltower/latest/userguide/table-of-baselines.html
#   baseline 1.0 -> LZ 2.0-2.7   baseline 2.0 -> LZ 2.8-2.9   baseline 3.0 -> LZ 3.0-3.1
#   baseline 4.0 -> LZ 3.2-3.3   baseline 5.0 -> LZ 4.0
# --------------------------------------------------------------------------- #
def version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for comp in str(v or "").strip().split("."):
        comp = comp.strip()
        if not comp:
            continue
        if not comp.isdigit():
            return tuple()
        parts.append(int(comp))
    return tuple(parts)


def normalize_version(v: str) -> str:
    t = version_tuple(v)
    return ".".join(str(x) for x in t) if t else ""


def _in_range(v: str, lo: str, hi: str) -> bool:
    tv, tlo, thi = version_tuple(v), version_tuple(lo), version_tuple(hi)
    return bool(tv and tlo and thi and tlo <= tv <= thi)


def target_baseline_version_for_lz(lz_version: str) -> str | None:
    """Latest AWSControlTowerBaseline version compatible with this landing zone."""
    if _in_range(lz_version, "2.0", "2.7"):
        return "1.0"
    if _in_range(lz_version, "2.8", "2.9"):
        return "2.0"
    if _in_range(lz_version, "3.0", "3.1"):
        return "3.0"
    if _in_range(lz_version, "3.2", "3.3"):
        return "4.0"
    if version_tuple(lz_version) == version_tuple("4.0"):
        return "5.0"
    return None


# --------------------------------------------------------------------------- #
# AWS reads
# --------------------------------------------------------------------------- #
def management_account_id(org) -> str | None:
    return (org.describe_organization().get("Organization") or {}).get(
        "ManagementAccountId"
    )


def list_child_ous(org, parent_id: str) -> list[dict]:
    out: list[dict] = []
    paginator = org.get_paginator("list_organizational_units_for_parent")
    for page in paginator.paginate(ParentId=parent_id):
        out.extend(page.get("OrganizationalUnits", []) or [])
    log.debug(
        "list_child_ous(%s) -> %d child OU(s): %s",
        parent_id,
        len(out),
        ", ".join(f"{o.get('Id')}({o.get('Name')})" for o in out) or "(none)",
    )
    return out


def ordered_ou_ids(org) -> list[str]:
    """All OU ids, top-down (parent before child), depth-first pre-order."""
    ordered: list[str] = []

    def walk(parent_id: str) -> None:
        for ou in list_child_ous(org, parent_id):
            ou_id = ou.get("Id")
            if not ou_id:
                continue
            ordered.append(ou_id)
            walk(ou_id)

    for root in org.list_roots().get("Roots", []) or []:
        if root.get("Id"):
            log.debug("Walking from root %s (%s)", root.get("Id"), root.get("Name"))
            walk(root["Id"])
    log.debug("Discovered %d OU(s) in top-down order: %s", len(ordered), ordered)
    return ordered


def ou_name(org, ou_id: str) -> str:
    try:
        resp = org.describe_organizational_unit(OrganizationalUnitId=ou_id)
        return (resp.get("OrganizationalUnit") or {}).get("Name") or ""
    except ClientError:
        return ""


def direct_account_ids(org, ou_id: str) -> set[str]:
    out: set[str] = set()
    detail: list[str] = []
    paginator = org.get_paginator("list_accounts_for_parent")
    for page in paginator.paginate(ParentId=ou_id):
        for acct in page.get("Accounts", []) or []:
            if acct.get("Id"):
                out.add(acct["Id"])
                detail.append(f"{acct['Id']}({acct.get('Name') or '?'})")
    log.debug(
        "OU %s has %d direct account(s): %s", ou_id, len(out), ", ".join(detail) or "(none)"
    )
    return out


def landing_zone_manifest(ct) -> dict:
    try:
        lzs = ct.list_landing_zones().get("landingZones", []) or []
    except ClientError as e:
        log.warning("ListLandingZones failed: %s", e)
        return {}
    if not lzs:
        return {}
    ident = lzs[0] if isinstance(lzs[0], str) else lzs[0].get("arn")
    if not ident:
        return {}
    log.debug("Using landing zone %s", ident)
    try:
        lz = ct.get_landing_zone(landingZoneIdentifier=ident).get("landingZone") or {}
    except ClientError as e:
        log.warning("GetLandingZone failed: %s", e)
        return {}
    log.debug("Landing zone version: %s", lz.get("version") or "?")
    manifest = lz.get("manifest") or {}
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except ValueError:
            manifest = {}
    manifest["_version"] = str(lz.get("version") or "")
    return manifest


def detect_core_ou(org, ct) -> str | None:
    """Find the Control Tower core/security OU from the landing zone manifest.

    Same heuristic as the template: find the OU whose direct accounts contain
    both the security and centralized-logging accounts named in the manifest.
    """
    manifest = landing_zone_manifest(ct)
    security = (manifest.get("securityRoles") or {}).get("accountId", "").strip()
    logging_ = (manifest.get("centralizedLogging") or {}).get("accountId", "").strip()
    mgmt = management_account_id(org)

    log.debug(
        "Manifest core accounts: security=%s logging=%s mgmt=%s", security or "?", logging_ or "?", mgmt or "?"
    )
    needle = {a for a in (security, logging_) if a and a != mgmt}
    if not needle:
        log.warning("Could not read core account ids from landing zone manifest.")
        return None

    log.debug("Searching for core OU containing accounts %s", sorted(needle))
    for ou_id in ordered_ou_ids(org):
        if needle.issubset(direct_account_ids(org, ou_id)):
            log.info("Detected core OU %s (%s) from security/logging accounts.", ou_id, ou_name(org, ou_id))
            return ou_id

    log.warning("Core OU not found from manifest accounts %s.", sorted(needle))
    return None


def enabled_baselines_by_ou(ct) -> dict[str, list[dict]]:
    """Map OU id -> list of enabled-baseline summaries targeting that OU."""
    inventory: dict[str, list[dict]] = {}
    token = None
    total = 0
    while True:
        kwargs = {"nextToken": token} if token else {}
        resp = ct.list_enabled_baselines(**kwargs)
        for summary in resp.get("enabledBaselines", []) or []:
            total += 1
            target = summary.get("targetIdentifier")
            ou_id = parse_ou_id(target)
            if ou_id:
                inventory.setdefault(ou_id, []).append(summary)
                log.debug(
                    "Enabled baseline on OU %s: id=%s version=%s status=%s",
                    ou_id,
                    enabled_baseline_id(summary),
                    enabled_baseline_version(summary) or "?",
                    baseline_status(summary) or "?",
                )
            else:
                log.debug("Skipping non-OU enabled baseline target: %s", target)
        token = resp.get("nextToken")
        if not token:
            break
    log.debug("Enabled-baseline inventory: %d baseline(s) across %d OU(s)", total, len(inventory))
    return inventory


def landing_zone_version(ct) -> str:
    return normalize_version(landing_zone_manifest(ct).get("_version") or "")


def aws_controltower_baseline_arn(ct) -> str | None:
    """ARN of the 'AWSControlTowerBaseline' baseline definition, if discoverable."""
    token = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        resp = ct.list_baselines(**kwargs)
        for b in resp.get("baselines", []) or []:
            if (b.get("name") or "").strip() == "AWSControlTowerBaseline":
                return b.get("arn")
        token = resp.get("nextToken")
        if not token:
            return None


def is_ct_baseline(summary: dict, ct_baseline_arn: str | None) -> bool:
    bid = baseline_identifier(summary)
    if ct_baseline_arn:
        return bid == ct_baseline_arn
    # Fallback if the definition ARN couldn't be resolved.
    return "AWSControlTowerBaseline" in bid


def enabled_baseline_parameters(ct, enabled_id: str) -> list[dict]:
    """Existing parameters on an enabled baseline, as [{key, value}], to preserve on update."""
    try:
        resp = ct.get_enabled_baseline(enabledBaselineIdentifier=enabled_id)
    except ClientError as e:
        log.warning("GetEnabledBaseline failed for %s: %s", enabled_id, e)
        return []
    details = resp.get("enabledBaselineDetails") or {}
    out = []
    for p in details.get("parameters") or []:
        key = (p.get("key") or "").strip()
        value = p.get("value")
        if key and value not in (None, ""):
            out.append({"key": key, "value": value})
    return out


# --------------------------------------------------------------------------- #
# AWS writes (reset / update + poll)
# --------------------------------------------------------------------------- #
class OperationFailed(RuntimeError):
    pass


def _start_with_conflict_wait(fn, kwargs: dict, label: str, conflict_wait: int, conflict_attempts: int) -> str:
    """Call a *_enabled_baseline API, retrying while another operation is in progress."""
    for attempt in range(1, conflict_attempts + 1):
        try:
            log.debug("%s(%s) attempt %d", label, kwargs.get("enabledBaselineIdentifier"), attempt)
            resp = fn(**kwargs)
            return resp.get("operationIdentifier") or ""
        except ClientError as e:
            if is_conflict_in_progress(e) and attempt < conflict_attempts:
                log.info(
                    "  baseline busy (another operation in progress); "
                    "waiting %ss then retry %d/%d",
                    conflict_wait,
                    attempt + 1,
                    conflict_attempts,
                )
                time.sleep(conflict_wait)
                continue
            raise
    raise OperationFailed(f"{label} still blocked after {conflict_attempts} attempts")


def reset_baseline(ct, enabled_id: str, conflict_wait: int, conflict_attempts: int) -> str:
    return _start_with_conflict_wait(
        ct.reset_enabled_baseline,
        {"enabledBaselineIdentifier": enabled_id},
        "reset_enabled_baseline",
        conflict_wait,
        conflict_attempts,
    )


def update_baseline(
    ct,
    enabled_id: str,
    target_version: str,
    parameters: list[dict],
    conflict_wait: int,
    conflict_attempts: int,
) -> str:
    kwargs: dict = {"enabledBaselineIdentifier": enabled_id, "baselineVersion": target_version}
    if parameters:
        kwargs["parameters"] = parameters
    return _start_with_conflict_wait(
        ct.update_enabled_baseline,
        kwargs,
        "update_enabled_baseline",
        conflict_wait,
        conflict_attempts,
    )


def wait_for_operation(ct, operation_id: str, poll_interval: int, timeout: int) -> dict:
    deadline = time.time() + timeout
    start = time.time()
    while True:
        op = ct.get_baseline_operation(operationIdentifier=operation_id).get(
            "baselineOperation"
        ) or {}
        status = str(op.get("status") or "").upper()
        log.debug(
            "  operation %s status=%s after %ds",
            operation_id,
            status or "UNKNOWN",
            int(time.time() - start),
        )
        if status == "SUCCEEDED":
            return op
        if status == "FAILED":
            raise OperationFailed(op.get("statusMessage") or "operation FAILED")
        if time.time() > deadline:
            raise OperationFailed(
                f"timed out after {timeout}s waiting for operation {operation_id} "
                f"(last status {status or 'UNKNOWN'})"
            )
        time.sleep(poll_interval)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
@dataclass
class BaselineAction:
    enabled_id: str
    kind: str  # "reset" or "update"
    current_version: str
    target_version: str = ""
    parameters: list[dict] = field(default_factory=list)


@dataclass
class OUPlan:
    ou_id: str
    name: str
    action: str  # "apply" or "skip"
    reason: str = ""
    baselines: list[BaselineAction] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)


def resolve_target(org, raw: str) -> str:
    """Resolve --target given either an OU id or an OU name to a single OU id."""
    parsed = parse_ou_id(raw)
    if parsed:
        return parsed

    wanted = raw.strip().lower()
    matches = [ou_id for ou_id in ordered_ou_ids(org) if ou_name(org, ou_id).strip().lower() == wanted]
    if not matches:
        raise SystemExit(f"--target {raw!r}: no OU found with that id or name.")
    if len(matches) > 1:
        pretty = ", ".join(f"{m} ({ou_name(org, m)})" for m in matches)
        raise SystemExit(
            f"--target {raw!r} matches multiple OUs: {pretty}. Re-run using the OU id."
        )
    return matches[0]


def _plan_ou_baselines(
    ct,
    ou_id: str,
    enabled: list[dict],
    *,
    lz_version: str,
    ct_baseline_arn: str | None,
    upgrade: bool,
    version_override: str | None,
) -> tuple[list[BaselineAction], str | None]:
    """Decide reset vs update per enabled baseline. Returns (actions, blocked_reason)."""
    actions: list[BaselineAction] = []
    for s in enabled:
        eid = enabled_baseline_id(s)
        if not eid:
            continue
        cur = normalize_version(enabled_baseline_version(s))

        if not is_ct_baseline(s, ct_baseline_arn):
            actions.append(BaselineAction(eid, "reset", cur, cur))
            continue

        target = normalize_version(version_override or target_baseline_version_for_lz(lz_version) or "")

        if not target:
            # Landing zone version not in our table and no override: best-effort reset.
            log.warning(
                "OU %s: landing zone version %s not in the compatibility table; attempting a "
                "plain reset of baseline v%s. Pass --baseline-version to force an upgrade target.",
                ou_id, lz_version or "?", cur or "?",
            )
            actions.append(BaselineAction(eid, "reset", cur, cur))
        elif cur == target:
            actions.append(BaselineAction(eid, "reset", cur, target))
        elif upgrade:
            params = enabled_baseline_parameters(ct, eid)
            actions.append(BaselineAction(eid, "update", cur, target, params))
        else:
            return [], (
                f"baseline v{cur or '?'} incompatible with landing zone v{lz_version} "
                f"(needs v{target}); re-run with --upgrade"
            )

    return actions, None


def build_plan(
    org,
    ct,
    *,
    skip: set[str],
    target: str | None,
    completed: set[str],
    upgrade: bool,
    version_override: str | None,
) -> list[OUPlan]:
    inventory = enabled_baselines_by_ou(ct)
    core_ou = detect_core_ou(org, ct)
    ordered = ordered_ou_ids(org)
    lz_version = landing_zone_version(ct)
    ct_baseline_arn = aws_controltower_baseline_arn(ct)
    log.info("Landing zone version %s; AWSControlTowerBaseline needs v%s.",
             lz_version or "?", target_baseline_version_for_lz(lz_version) or "?(unknown)")

    if target:
        ordered = [o for o in ordered if o == target]
        if not ordered:
            log.warning("--target %s is not an OU in this organization.", target)

    plans: list[OUPlan] = []
    for ou_id in ordered:
        name = ou_name(org, ou_id)
        log.debug("Evaluating OU %s (%s)", ou_id, name or "(no name)")

        def decide(action: str, reason: str, baselines=None, accounts=None) -> None:
            log.debug("  decision for %s: %s (%s)", ou_id, action.upper(), reason)
            plans.append(OUPlan(ou_id, name, action, reason, baselines or [], accounts or []))

        if target is None and ou_id in completed:
            decide("skip", "already-completed (state file)")
            continue
        if ou_id in skip:
            decide("skip", "in --skip list")
            continue
        if core_ou and ou_id == core_ou:
            decide("skip", "core/security OU")
            continue

        enabled = inventory.get(ou_id) or []
        if not enabled:
            decide("skip", "not Control Tower managed")
            continue

        busy = [s for s in enabled if baseline_status(s) in BUSY_BASELINE_STATUSES]
        if busy:
            decide("skip", "baseline under change")
            continue

        actions, blocked = _plan_ou_baselines(
            ct, ou_id, enabled,
            lz_version=lz_version, ct_baseline_arn=ct_baseline_arn,
            upgrade=upgrade, version_override=version_override,
        )
        if blocked:
            decide("skip", blocked)
            continue
        if not actions:
            decide("skip", "no resettable baseline id")
            continue

        accounts = sorted(direct_account_ids(org, ou_id))
        decide("apply", "managed", actions, accounts)

    return plans


def _baseline_label(b: BaselineAction) -> str:
    if b.kind == "update":
        return f"UPDATE v{b.current_version or '?'}->v{b.target_version}"
    return f"reset v{b.current_version or '?'}"


def print_plan(plans: list[OUPlan]) -> None:
    act = [p for p in plans if p.action == "apply"]
    skip = [p for p in plans if p.action == "skip"]
    n_update = sum(1 for p in act for b in p.baselines if b.kind == "update")

    print("\n=== Plan ===")
    extra = f"   (baseline upgrades: {n_update})" if n_update else ""
    print(f"OUs discovered: {len(plans)}   to act: {len(act)}   to skip: {len(skip)}{extra}\n")

    if act:
        print("Will ACT (in this order):")
        for i, p in enumerate(act, 1):
            detail = "; ".join(_baseline_label(b) for b in p.baselines)
            print(
                f"  {i:>3}. {p.ou_id}  {p.name or '(no name)'}  "
                f"[{detail}]  ({len(p.accounts)} account(s))"
            )
            log.debug("       accounts in %s: %s", p.ou_id, ", ".join(p.accounts) or "(none)")
    else:
        print("Will ACT: (nothing)")

    if skip:
        print("\nWill SKIP:")
        for p in skip:
            print(f"       {p.ou_id}  {p.name or '(no name)':<28}  — {p.reason}")
    print()


# --------------------------------------------------------------------------- #
# State file (resumability)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {"completed": [], "failed": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("completed", [])
        data.setdefault("failed", {})
        return data
    except (ValueError, OSError) as e:
        log.warning("Could not read state file %s (%s); starting fresh.", path, e)
        return {"completed": [], "failed": {}}


def save_state(path: str, state: dict) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
    except OSError as e:
        log.warning("Could not write state file %s: %s", path, e)


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def apply_plan(ct, plans: list[OUPlan], args, state: dict) -> int:
    to_apply = [p for p in plans if p.action == "apply"]
    completed = set(state.get("completed", []))
    failures = 0

    for i, p in enumerate(to_apply, 1):
        label = f"[{i}/{len(to_apply)}] {p.ou_id} {p.name or ''}".strip()
        print(f"\n>>> {label}")
        print(f"    {len(p.accounts)} account(s) in this OU: {', '.join(p.accounts) or '(none)'}")
        log.debug("Acting on OU %s (%s); baselines=%d accounts=%s", p.ou_id, p.name, len(p.baselines), p.accounts)
        try:
            for b in p.baselines:
                if b.kind == "update":
                    print(f"    UPDATE baseline {b.enabled_id} v{b.current_version or '?'} -> v{b.target_version} ...")
                    op_id = update_baseline(
                        ct, b.enabled_id, b.target_version, b.parameters,
                        args.conflict_wait, args.conflict_attempts,
                    )
                else:
                    print(f"    reset baseline {b.enabled_id} (v{b.current_version or '?'}) ...")
                    op_id = reset_baseline(
                        ct, b.enabled_id, args.conflict_wait, args.conflict_attempts
                    )
                print(f"    operation {op_id} started; polling every {args.poll_interval}s ...")
                op = wait_for_operation(ct, op_id, args.poll_interval, args.poll_timeout)
                print(f"    SUCCEEDED ({op.get('operationType', b.kind.upper())})")

            completed.add(p.ou_id)
            state["completed"] = sorted(completed)
            state.get("failed", {}).pop(p.ou_id, None)
            save_state(args.state_file, state)

        except (ClientError, OperationFailed) as e:
            failures += 1
            msg = str(e)
            log.error("OU %s failed: %s", p.ou_id, msg)
            state.setdefault("failed", {})[p.ou_id] = msg
            save_state(args.state_file, state)
            if not args.ignore_errors:
                print(
                    f"\n!!! Stopping: OU {p.ou_id} failed and --ignore-errors was not set.\n"
                    f"    Fix the issue, then re-run (completed OUs are skipped via the state file)."
                )
                return failures
            print(f"    FAILED (continuing because --ignore-errors): {msg}")

    return failures


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_skip(raw: str | None) -> set[str]:
    out: set[str] = set()
    bad: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        if OU_ID_RE.match(item):
            out.add(item)
        else:
            bad.append(item)
    if bad:
        raise SystemExit(f"Invalid --skip OU id(s): {', '.join(bad)}")
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Sequentially reset (and optionally upgrade) the Control Tower baseline for every managed OU.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--region", help="Control Tower HOME region (defaults to the session region).")
    p.add_argument("--profile", help="AWS profile / SSO profile for the management account.")
    p.add_argument(
        "--target",
        help="Only process this single OU, by id (ou-xxxx-xxxxxxxx) OR by name. "
        "Ideal for testing on one OU first. Still respects the core-OU guard.",
    )
    p.add_argument("--skip", help="Comma-separated OU ids to skip entirely.")
    p.add_argument(
        "--upgrade",
        action="store_true",
        help="For OUs whose AWSControlTowerBaseline is behind the landing zone, call "
        "UpdateEnabledBaseline to the compatible version instead of skipping. "
        "NOTE: baseline updates cannot be rolled back. Without this flag such OUs are skipped.",
    )
    p.add_argument(
        "--baseline-version",
        help="Override the target baseline version for --upgrade (e.g. 5.0). Use only if the "
        "built-in compatibility table doesn't cover your landing zone version.",
    )
    p.add_argument("--apply", action="store_true", help="Actually reset/update (default is dry-run).")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before --apply.")
    p.add_argument(
        "--ignore-errors",
        action="store_true",
        help="Continue to the next OU when an OU fails (default: stop).",
    )
    p.add_argument(
        "--state-file",
        default="ct_reregister_state.json",
        help="Local JSON progress file for resumability (default: ./ct_reregister_state.json).",
    )
    p.add_argument("--restart", action="store_true", help="Ignore completed OUs in the state file.")
    p.add_argument("--poll-interval", type=int, default=30, help="Seconds between status polls (default 30).")
    p.add_argument(
        "--poll-timeout",
        type=int,
        default=7200,
        help="Max seconds to wait for one baseline operation (default 7200).",
    )
    p.add_argument("--conflict-wait", type=int, default=30, help="Seconds to wait on ConflictException (default 30).")
    p.add_argument("--conflict-attempts", type=int, default=20, help="Max conflict retries (default 20).")
    p.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return p


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    skip = parse_skip(args.skip)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = args.region or session.region_name
    if not region:
        raise SystemExit("No region set. Pass --region (the Control Tower home region).")

    # Organizations is global; Control Tower is regional (home region).
    org = session.client("organizations", config=BOTO_CFG)
    ct = session.client("controltower", region_name=region, config=BOTO_CFG)

    ident = session.client("sts").get_caller_identity()
    log.info("Account %s as %s, Control Tower region %s", ident["Account"], ident["Arn"], region)

    target = None
    if args.target:
        target = resolve_target(org, args.target)
        log.info("Single-OU mode: only %s (%s) will be processed.", target, ou_name(org, target))

    state = load_state(args.state_file)
    completed = set() if args.restart else set(state.get("completed", []))
    if completed:
        log.info("State file lists %d already-completed OU(s); they will be skipped.", len(completed))

    if args.baseline_version and not args.upgrade:
        log.warning("--baseline-version is ignored without --upgrade.")

    plans = build_plan(
        org, ct,
        skip=skip, target=target, completed=completed,
        upgrade=args.upgrade,
        version_override=normalize_version(args.baseline_version) if args.baseline_version else None,
    )
    print_plan(plans)

    to_apply = [p for p in plans if p.action == "apply"]
    if not to_apply:
        print("Nothing to do.")
        return 0

    if not args.apply:
        print("DRY-RUN: no changes made. Re-run with --apply to perform the actions above.")
        return 0

    n_update = sum(1 for p in to_apply for b in p.baselines if b.kind == "update")
    if not args.yes:
        warn_update = (
            f" {n_update} of them are baseline UPGRADES, which cannot be rolled back."
            if n_update else ""
        )
        print(
            f"About to act on {len(to_apply)} OU(s) in account {ident['Account']} "
            f"(region {region}). This re-applies Control Tower guardrails across every "
            f"account in those OUs.{warn_update}"
        )
        if input("Type 'yes' to continue: ").strip().lower() != "yes":
            print("Aborted.")
            return 1

    failures = apply_plan(ct, plans, args, state)
    done = len(state.get("completed", []))
    print(f"\n=== Done. {done} OU(s) completed, {failures} failure(s). ===")
    if state.get("failed"):
        print("Failed OUs:")
        for ou_id, msg in state["failed"].items():
            print(f"  {ou_id}: {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run to resume (completed OUs are skipped).")
        sys.exit(130)
