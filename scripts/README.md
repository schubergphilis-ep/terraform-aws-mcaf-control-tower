# scripts

Operational helper scripts for this Control Tower landing zone. Run by hand by an operator — they are not part of the Terraform module.

| Script | What it does |
| --- | --- |
| [`update_control_tower_accounts.py`](#update_control_tower_accountspy) | Updates enrolled **accounts** that show "Update available", five at a time. |
| [`reregister_control_tower_ous.py`](#reregister_control_tower_ouspy) | Re-registers (resets the baseline of) managed **OUs**, one at a time. |

## Before you run either script

1. **Have valid AWS credentials for the Control Tower management account**, in the
   **Control Tower home region**. Needs Control Tower / Service Catalog / Organizations admin rights.
2. Have [uv](https://docs.astral.sh/uv/) installed. Both scripts carry inline
   metadata (PEP 723), so uv takes care of Python and all dependencies
   automatically — there is nothing else to install.

Shared behaviour of both scripts:

- **Nothing happens without `--apply`.** Leaving it off is always a dry run that
  prints the plan and changes nothing.
- With `--apply` they show the plan and ask you to type `yes` to confirm; `--yes`
  skips the prompt (e.g. in a pipeline).
- `--region` picks the Control Tower region, `--profile` an AWS named profile, and
  `--help` lists every option.
- They are safe to re-run: work that is already done is skipped.
- If a script finds no accounts / no managed OUs, double-check you're in the
  **management account** and the **Control Tower home region**.

## `update_control_tower_accounts.py`

After you upgrade the Control Tower landing zone or modify its configuration,
enrolled accounts show **"Update available"** in the console and each one has to
be updated. This script does that for you, in batches of five, instead of clicking **Update account** on every account by hand.

**Step 1 — see what would be updated (safe, changes nothing):**

```bash
uv run scripts/update_control_tower_accounts.py
```

This prints the list of accounts that have an update available. Use it to check the list looks right.

**Step 2 — do the update:**

```bash
uv run scripts/update_control_tower_accounts.py --apply
```

After you confirm, it updates the accounts five at a time and reports whether each
one succeeded or failed.

### Common variations

Only update accounts in certain OUs (use the OU name or id):

```bash
uv run scripts/update_control_tower_accounts.py --apply \
    --include-ou Workloads --include-ou Sandbox
```

Update everything **except** some OUs:

```bash
uv run scripts/update_control_tower_accounts.py --apply \
    --exclude-ou Foundation --exclude-ou Security
```

Leave specific accounts out of the run (name or account id):

```bash
uv run scripts/update_control_tower_accounts.py --apply \
    --exclude-account sandbox --exclude-account 361769582329
```

Use a specific AWS profile and skip the confirmation prompt:

```bash
uv run scripts/update_control_tower_accounts.py --apply \
    --profile my-management-profile --yes
```

### Good to know

- **It only touches accounts that need it** — accounts already up to date are
  skipped automatically. If some accounts fail, just run it again; it picks up
  whatever still shows an update available.
- **It won't change your accounts' settings** — the update only advances the
  baseline version. It never changes an account's email, name, SSO user or OU.

## `reregister_control_tower_ous.py`

Sometimes it isn't the accounts that are out of sync but the **OUs**: guardrails
drift, a landing zone upgrade leaves OU baselines behind, or AWS support asks you
to "re-register" an OU. In the console that means clicking **Re-register OU** on
every OU in turn and waiting for each one to finish. This script walks the
organization's managed OUs top-down and does it for you, one OU at a time.

It is a lean local port of the aws-samples *ControlTower Organization
ReRegistration* CloudFormation automation — same idea, but driven synchronously
from your laptop instead of via EventBridge and Step Functions, and without the
optional-controls reset that template also performs.

What it does per OU:

- resets the enabled Control Tower baseline(s) on the OU, and waits for the
  operation to reach `SUCCEEDED` before moving to the next OU;
- with `--upgrade`, first raises a baseline that is behind the landing zone to
  the compatible version (a plain reset fails on those);
- records progress in a local JSON file so an interrupted run can be resumed.

**Set aside time.** Every OU is processed sequentially and a single baseline
operation can take a long while; the per-operation timeout defaults to two hours.
Run it in `tmux`/`screen` if you're on a flaky connection.

**Step 1 — see the plan (safe, changes nothing):**

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1
```

This prints every OU it discovered, which ones it would act on and in what order,
which ones it would skip and why, and how many accounts sit in each.

**Step 2 — try a single OU first:**

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply --target Sandbox
```

`--target` takes an OU **name or id**. Do this before a full run.

**Step 3 — do the whole organization:**

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply
```

After you confirm, it works through the OUs one at a time, polling each operation
to completion.

### Common variations

Skip specific OUs (comma-separated, **ids only** — names are not accepted here):

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply \
    --skip ou-ab12-11111111,ou-ab12-22222222
```

Also upgrade OUs whose baseline is behind the landing zone:

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply --upgrade
```

Keep going after an OU fails instead of stopping (the default is to stop):

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply --ignore-errors
```

Start over from scratch, ignoring the recorded progress:

```bash
uv run scripts/reregister_control_tower_ous.py --region eu-west-1 --apply --restart
```

`--help` also documents the polling and conflict-retry timings.

### Good to know

- **It protects the core OU.** The Control Tower core/security OU is detected
  from the landing zone manifest (the OU holding the security and log-archive
  accounts) and is always skipped, including in `--target` mode. OUs that aren't
  Control Tower managed, or whose baseline is mid-operation, are skipped too.
- **`--upgrade` is one-way.** Baseline *updates* cannot be rolled back, so
  without the flag such OUs are reported and skipped rather than changed. Existing
  baseline parameters are read back and re-sent unchanged on an upgrade.
- **`--baseline-version` only matters with `--upgrade`,** as an escape hatch when
  your landing zone version isn't in the built-in compatibility table
  ([table of baselines](https://docs.aws.amazon.com/controltower/latest/userguide/table-of-baselines.html)).
  It is ignored otherwise.
- **It's safe to interrupt.** Completed OUs are recorded in
  `./ct_reregister_state.json` (change with `--state-file`) and skipped next time;
  failed OUs are recorded there with their error message. `Ctrl-C` just stops it.
- **Re-registering re-applies guardrails across every account in the OU,** so
  expect activity in those accounts. It does not move accounts or change their
  settings.

## For contributors

Each script ships with a unit test suite next to it. All AWS calls are stubbed, so the tests run offline and never touch an account or OU:

```bash
uv run scripts/test_update_control_tower_accounts.py
uv run scripts/test_reregister_control_tower_ous.py
```

CI runs both suites automatically on every pull request that touches `scripts/`
(see `.github/workflows/test-scripts.yaml`). If you change a script, keep the
tests passing and add coverage for new behavior — especially the safety
guarantees.
