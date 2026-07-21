# scripts

Operational helper scripts for this Control Tower landing zone. Run by hand by an
operator — they are not part of the Terraform module.

## `update_control_tower_accounts.py`

After you upgrade the Control Tower landing zone or modify its configuration,
enrolled accounts show **"Update available"** in the console and each one has to
be updated. This script does that for you, in batches of five, instead of clicking **Update account** on every account by hand.

### Before you run it

1. **Have valid AWS credentials for the Control Tower management account**, in
   the **same region** as the Control Tower deployment, with admin rights.
2. Have [uv](https://docs.astral.sh/uv/) installed. The script carries inline
   metadata (PEP 723), so uv takes care of Python and all dependencies
   automatically — there is nothing else to install.

### How to use it

**Step 1 — see what would be updated (safe, changes nothing):**

```bash
uv run scripts/update_control_tower_accounts.py
```

This prints the list of accounts that have an update available. Nothing is
changed. Use this to check the list looks right.

**Step 2 — do the update:**

```bash
uv run scripts/update_control_tower_accounts.py --apply
```

It shows the list, asks you to type `yes` to confirm, then updates the accounts
five at a time and reports whether each one succeeded or failed.

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

Use a specific AWS profile, and don't ask for confirmation (e.g. in a pipeline):

```bash
uv run scripts/update_control_tower_accounts.py --apply \
    --profile my-management-profile --yes
```

Run `uv run scripts/update_control_tower_accounts.py --help` to see every option.

### Good to know

- **It only touches accounts that need it** — accounts already up to date are
  skipped automatically.
- **It's safe to re-run.** If some accounts fail, just run it again; it picks up
  whatever still shows an update available.
- **It won't change your accounts' settings** — the update only advances the
  baseline version. It never changes an account's email, name, SSO user or OU.
- **Nothing happens without `--apply`.** Leaving it off is always a dry run.
- If it can't find the accounts, double-check you're in the **management account**
  and the **correct region**, and that your login has Control Tower / Service
  Catalog admin rights.

### For contributors

The script ships with a unit test suite in
[`test_update_control_tower_accounts.py`](./test_update_control_tower_accounts.py).
All AWS calls are stubbed, so the tests run offline and never touch an account:

```bash
uv run scripts/test_update_control_tower_accounts.py
```

CI runs the suite automatically on every pull request that touches `scripts/`
(see `.github/workflows/test-scripts.yaml`). If you change the script, keep the
tests passing and add coverage for new behavior — especially anything touching
the safety guarantees (dry-run default, re-sending each account's current
parameter values unchanged, OU filter fail-safe, batch limit).
