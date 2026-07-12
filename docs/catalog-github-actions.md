# GitHub Actions — daily workflow

The [daily workflow](../.github/workflows/catalog-daily-workflow.yml) runs the Airtable production orchestrator (`python -m catalog_parser`): editor assignment, Drive media mixing, catalog ingest from Google Sheets, and sync-done cleanup. Humans update **Status** and translated fields in Airtable; the bot does not read or write comments.

## Schedule and timezone

GitHub Actions **always runs `cron` in UTC**. You **cannot** put the schedule time in an environment variable or secret — the expression must be a literal string in the workflow file.

| Local time (UTC+3) | UTC cron minute/hour | Workflow line |
|------------------|----------------------|---------------|
| Midnight         | `0 21 * * *`         | Default in repo |
| 06:00            | `0 3 * * *`          | Edit `.github/workflows/catalog-daily-workflow.yml` |

To change the run time, edit the `cron:` line and push. Use [crontab.guru](https://crontab.guru/#0_21_*_*_*) to validate expressions (remember: UTC, not your local zone).

## Turning automatic runs on or off

| Method | Effect |
|--------|--------|
| **Actions → workflow name → Disable workflow** | Stops all triggers (schedule and manual) until re-enabled in the UI. |
| **Remove the `schedule:` block** | Permanent opt-out in git; only `workflow_dispatch` remains. |
| **Settings → Actions → Disable actions** | Disables every workflow in the repository. |

### Manual run

**Actions → Daily catalog workflow → Run workflow**

- Leave **dry_run** unchecked for a real run.
- Check **dry_run** to print planned actions without writing to Airtable or Drive.

## Airtable backups and status history

Each orchestrator run writes a full-table JSON snapshot to `output/backups/`:

- `airtable-YYYY-MM-DD.json` — dated backup for that run day
- `airtable-latest.json` — most recent backup (used as the baseline for the next run's status diff)
- `airtable-previous.json` — copy of the prior `airtable-latest.json` before each run

Status changes between daily snapshots are appended to `output/workflow/status_history.json`. This file powers the weekly report and requires **no extra Airtable API calls**.

On GitHub Actions, the daily workflow:

1. Restores `workflow-state` from the latest successful daily run (status history + previous backup).
2. Runs the orchestrator and appends any new status events.
3. Uploads `airtable-latest.json` and the accumulated `status_history.json` as artifacts.

Download artifacts from the workflow run page under **Artifacts**.

Restore locally:

```powershell
# Preview differences
python scripts/catalog/restore_airtable_backup.py --backup path\to\airtable-latest.json

# Apply field updates and recreate missing records
python scripts/catalog/restore_airtable_backup.py --backup path\to\airtable-latest.json --apply
```

`--delete-missing --confirm-delete` can remove live records that are not in the backup. Use only when you intentionally want the backup to be the full source of truth.

## Repository secrets

Configure under **Settings → Secrets and variables → Actions → Secrets**.

### Required

| Secret | Description |
|--------|-------------|
| `AIRTABLE_TOKEN` | [Airtable personal access token](https://airtable.com/create/tokens) with `data.records:read` and `data.records:write` on the target base. Comment scopes are not required.
| `AIRTABLE_BASE_ID` | Base id, e.g. `appbIH4wzW6ZRUnF5`. |
| `AIRTABLE_TABLE_NAME` | Table name exactly as shown in Airtable, e.g. `Translator's Paradise`. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service account key JSON (single line is fine). The service account must have access to the catalog sheet, video folders, Docs/Word files in those folders, and the output folder. |

### Repository variables (non-secret config)

These can live under **Settings → Secrets and variables → Actions → Variables** (or as secrets if you prefer):

| Variable | Description |
|----------|-------------|
| `WORKFLOW_PROFILES_JSON` | JSON object with `translators` and `editors` arrays (see below). |
| `OUTPUT_DRIVE_FOLDER` | Google Drive folder URL (or id) where combined media is uploaded, e.g. `https://drive.google.com/drive/folders/1sE-DZV2lrRJxEK7Fnjw7uU8y0KXg7imd`. |

The workflow checks **Variables first**, then **Secrets** for these two names.

### Required when ingest runs (web session mode)

Ingest uses Playwright with a saved Smartcat browser session (no company API required). Set:

| Secret | Description |
|--------|-------------|
| `SMARTCAT_STORAGE_STATE_JSON` | Full contents of local `smartcat-state.json` (from `python -m catalog_parser --smartcat-login`). |
| `GMAIL_SMTP_USER` | Gmail address used to send alert emails (e.g. your workspace Gmail). |
| `GMAIL_SMTP_APP_PASSWORD` | [Gmail App Password](https://myaccount.google.com/apppasswords) for that account (not your normal login password). |

Alert emails go to `georgi.uzunov-ext@sadhguru.org` (configured in the workflow). Also set:

| Secret | Description |
|--------|-------------|
| `SHEET_ID` | Google Sheet id from the catalog URL. |

### Optional: Canva thumbnail export during ingest

When a video folder has no direct thumbnail image, ingest can export the design from a Canva link in the docx. Uses the **same Canva integration** as publishing (`CANVA_*` secrets → `credentials/canva-token.json`).

| Secret | Description |
|--------|-------------|
| `CANVA_CLIENT_ID` | Canva Connect integration client id (shared with publish workflow). |
| `CANVA_CLIENT_SECRET` | Canva Connect integration client secret. |
| `CANVA_TOKEN_JSON` | Full contents of `credentials/canva-token.json`. |
| `CANVA_TOKEN_SYNC_PAT` | Fine-grained GitHub PAT with **Secrets: Read and write** on this repo. After CI refreshes the Canva token, the app updates `CANVA_TOKEN_JSON` automatically. |

If Canva secrets are missing, ingest still works for folders with a thumbnail image file in Drive. Canva-only thumbnails are skipped with an error on that record.

Renew locally (either CLI works — same token file):

```powershell
python -m media_publisher --canva-auth
python -m media_publisher --canva-auth-code <authorization-code>
```

### Smartcat session expiration alerts

Before each run, job `check-smartcat-session` validates `SMARTCAT_STORAGE_STATE_JSON`. If the session is expired:

1. An email is sent via Gmail SMTP with renewal instructions.
2. The main workflow job is skipped (ingest would fail anyway).

Renew the session locally:

```powershell
python -m catalog_parser --smartcat-login
```

Then update the `SMARTCAT_STORAGE_STATE_JSON` secret with the new file contents.

Test locally:

```powershell
python scripts/catalog/test_smartcat_session.py
python scripts/catalog/send_notification_email.py --subject "test" --body "test message"
```

(`GMAIL_SMTP_USER` and `GMAIL_SMTP_APP_PASSWORD` must be set in the environment for the email test.)

### Optional: Smartcat company API (alternative to web session)

If you have a company Smartcat account with API access, you can use API mode instead of `SMARTCAT_STORAGE_STATE_JSON` by setting `SMARTCAT_API=true` in the workflow and these secrets:

| Secret | Description |
|--------|-------------|
| `SMARTCAT_ACCOUNT_ID` | Smartcat company account id (Settings → API). |
| `SMARTCAT_API_KEY` | API key from Settings → API. |

Freelancer Smartcat accounts cannot use API mode.

### `WORKFLOW_PROFILES_JSON` example

Paste as one secret value (minified JSON):

```json
{
  "translators": [
    {
      "name": "Translator Name",
      "weekly_capacity_reels": 30,
      "preferred_translation_type": "Reel"
    }
  ],
  "editors": [
    {
      "name": "Editor Name",
      "weekly_capacity_reels": 30,
      "preferred_editing_type": "Video"
    }
  ]
}
```

Field names must match Airtable **Translator** / **Editor** single-select values.

## Repository variables (optional)

Variables are non-secret configuration under **Settings → Secrets and variables → Actions → Variables**.

Optional workflow tuning can be passed as **Variables** (or added to the workflow `env:` block) if you extend the workflow file:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKFLOW_REEL_TO_VIDEO_RATIO` | `6` | Target reel:video ratio for ingest. |
| `WORKFLOW_MAX_VIDEO_SECONDS` | `900` | Prefer videos under 15 minutes during ingest. |
| `SHEET_NAME` | first tab | Sheet tab name for ingest. |
| `SHEET_RANGE` | all used cells | A1 range for ingest. |
| `VIDEO_TYPE` | `Reel` | Default video type when not driven by workflow rules. |
| `SMARTCAT_TARGET_LANGUAGE` | `bg` | Smartcat language for subtitle checks. |

## Google service account setup

1. Create a service account in [Google Cloud Console](https://console.cloud.google.com/) for the same project as your APIs.
2. Enable **Google Sheets API**, **Google Drive API**, and **Google Docs API**.
3. Create a JSON key and store the entire file contents in `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. Share the catalog Google Sheet with the service account email (`...@....iam.gserviceaccount.com`) as **Viewer**.
5. Share each Drive folder used by the workflow (video folders, output folder) with that email as **Editor** (upload/delete needed for combined media).

OAuth `credentials.json` / `token.json` are for local development only; CI uses the service account.

## Weekly translation & editing report

The [weekly work report workflow](../.github/workflows/catalog-weekly-work-report.yml) emails a summary every **Monday at 09:00 UTC+3** (06:00 UTC).

The report covers the **previous calendar week** (Monday 00:00 – Sunday 23:59, UTC+3). It reads `output/workflow/status_history.json` accumulated by daily runs — **no Airtable API calls**.

- **Translation** — record entered `2. Translation done` without an Editor (Translator field used for attribution)
- **Editing** — record entered `3. Editing done` without Combined Media File (Editor field used for attribution)

If a record jumps from `1. To do` to `3. Editing done` between daily snapshots, translation and editing are credited separately in the same week.

Recipient: `georgi.uzunov-ext@sadhguru.org` (set in the workflow `NOTIFY_EMAIL` env).

Uses Gmail SMTP secrets only: `GMAIL_SMTP_USER`, `GMAIL_SMTP_APP_PASSWORD`.

Test locally:

```powershell
python scripts/catalog/send_weekly_work_report.py --dry-run
python scripts/catalog/send_weekly_work_report.py --send
```

## Checklist before first scheduled run

1. Add all required secrets.
2. Run **workflow_dispatch** with **dry_run** checked and confirm planned actions look correct.
3. Run **workflow_dispatch** without dry_run once.
4. Wait for the next scheduled run or adjust the cron time if needed.
