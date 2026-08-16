# GitHub Actions — daily workflow

The [daily workflow](../.github/workflows/catalog-daily-workflow.yml) runs the Airtable production orchestrator (`python -m catalog_parser`): editor assignment, Drive media mixing, and catalog ingest from Google Sheets. Humans update **Status** and translated fields in Airtable; the bot does not read or write comments. Combined media cleanup runs in the **publish** workflow after a video is successfully published. The job has a **60-minute** timeout so a hung run is cancelled instead of sitting for hours.

## Schedule and timezone

GitHub Actions **always runs `cron` in UTC**. You **cannot** put the schedule time in an environment variable or secret — the expression must be a literal string in the workflow file.

| Local time (UTC+3) | UTC cron minute/hour | Workflow line |
|------------------|----------------------|---------------|
| Midnight         | `0 21 * * *`         | Default in repo |
| 06:00            | `0 3 * * *`          | Edit `.github/workflows/catalog-daily-workflow.yml` |

To change the run time, edit the `cron:` line and push. Use [crontab.guru](https://crontab.guru/#0_21_*_*_*) to validate expressions (remember: UTC, not your local zone).

### External scheduler (recommended for on-time runs)

GitHub's built-in `schedule` event can slip by hours during peak traffic. For reliable midnight (or any fixed local time), use an external cron service to call the GitHub API and trigger `workflow_dispatch`.

[cron-job.org](https://cron-job.org) is free and supports time zones, so you can schedule **00:00 Europe/Sofia** directly instead of converting to UTC.

#### 1. Create a GitHub token

**Settings → Developer settings → Personal access tokens**

| Token type | Permissions |
|------------|-------------|
| Fine-grained | Repository access: this repo only. **Actions: Read and write**, **Contents: Read**. |
| Classic | Scope: **`repo`** (private repo) or **`public_repo`** (public only). |

Copy the token once (`ghp_...` or `github_pat_...`). Store it only in cron-job.org — do not commit it.

#### 2. Verify the API call locally (dry run)

Replace `OWNER`, `REPO`, and `YOUR_TOKEN`. This repo's default branch is **`master`** (not `main`).

```powershell
$headers = @{
  Authorization = "Bearer YOUR_TOKEN"
  Accept        = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}
$body = @{
  ref = "master"
  inputs = @{ dry_run = "true" }
} | ConvertTo-Json

Invoke-RestMethod `
  -Method POST `
  -Uri "https://api.github.com/repos/OWNER/REPO/actions/workflows/catalog-daily-workflow.yml/dispatches" `
  -Headers $headers `
  -Body $body `
  -ContentType "application/json"
```

Expected: HTTP **204 No Content** (empty response). Then open **Actions → Daily catalog workflow** — a new run should appear within seconds, triggered by `workflow_dispatch`, with **dry_run** enabled.

#### 3. Create the cron-job.org job

1. Sign up at [cron-job.org](https://console.cron-job.org/signup).
2. **Cronjobs → Create cronjob**.
3. **Title:** `catalog daily workflow`
4. **URL:** `https://api.github.com/repos/OWNER/REPO/actions/workflows/catalog-daily-workflow.yml/dispatches`
5. **Schedule:** enable **Custom** → crontab `0 0 * * *` → **Time zone: Europe/Sofia** (midnight local).
6. **Request method:** `POST`
7. **Request body** (JSON):

```json
{
  "ref": "master",
  "inputs": {
    "dry_run": "true"
  }
}
```

8. **Headers** (add each separately):

| Name | Value |
|------|-------|
| `Authorization` | `Bearer YOUR_TOKEN` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |

9. Save, then click **Run now** to test immediately.

#### 4. Confirm, then go live

1. In GitHub Actions, confirm the run starts within ~10 seconds of **Run now** (not hours later).
2. Check logs: dry run should print planned actions without writing to Airtable/Drive.
3. In cron-job.org, change `"dry_run": "true"` → `"false"`.
4. **Disable the GitHub `schedule` block** in `.github/workflows/catalog-daily-workflow.yml` so you do not get two runs per night (external cron + GitHub cron). Keep `workflow_dispatch` for manual runs.

#### 5. Optional: remove GitHub schedule in git

Comment out or delete the `schedule:` block:

```yaml
on:
  # schedule:
  #   - cron: "0 21 * * *"   # replaced by cron-job.org → workflow_dispatch
  workflow_dispatch:
    ...
```

Push to the branch cron-job uses in `"ref"` (`master` for this repo).

## Turning automatic runs on or off

| Method | Effect |
|--------|--------|
| **Actions → workflow name → Disable workflow** | Stops all triggers (schedule and manual) until re-enabled in the UI. |
| **Remove the `schedule:` block** | Permanent opt-out in git; only `workflow_dispatch` remains. |
| **Settings → Actions → Disable actions** | Disables every workflow in the repository. |

### Manual run

**Actions → Daily catalog workflow → Run workflow**

- **mode `full`** (default) — editor assignment, mixing, ingest, and the rest of the daily orchestrator.
  - Leave **dry_run** unchecked for a real run.
  - Check **dry_run** to print planned actions without writing to Airtable or Drive.
- **mode `ingest`** — create unassigned Airtable rows only (`7. Not Assigned`). Skips assignment, mixing, HappyScribe watch, and workflow-state artifacts.
  - **video_type** — `Reel`, `Short`, or `Video`
  - **count** — how many rows to ingest (default `4`)
  - **dry_run** — preview eligible rows without writing to Airtable

Local equivalent:

```powershell
python -m catalog_parser ingest --unassigned --type reel --count 4
```

Ingest reads the catalog spreadsheet from `catalog_id` in `workflow_config.json` (first sheet tab). It is not a GitHub secret or variable.

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
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service account key JSON (single line is fine). The service account must have access to the catalog sheet, video folders, Docs/Word files in those folders, and the output folder. |
| `TRANSLATION_API_KEY` | API key for the catalog RAG translator (Anthropic or OpenAI, matching `TRANSLATION_PROVIDER`). Required unless `TRANSLATION_PROVIDER` is `none`. |

### Archive duplicate-title checks during ingest

Ingest skips catalog rows whose title already exists in Airtable. Archive bases are discovered automatically from rows with Status **`7. Not Assigned`** whose Title looks like `2024 archive: https://airtable.com/...`. Each archive base can use a different title field (`Title` vs `Original Video Name`); the workflow detects that from the Airtable schema.

Archive titles are cached permanently at `output/backups/airtable-archive-titles.json` so ingest only queries each archive base once. Set `AIRTABLE_ARCHIVE_CACHE_REFRESH=true` to force a refresh, or delete the cache file.

### Repository variables (non-secret config)

These live under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Description |
|----------|-------------|
| `AIRTABLE_URL` | Share URL for the live catalog table, e.g. `https://airtable.com/appbIH4wzW6ZRUnF5/tblji1RaFztkeDn04/viw2Xz3EENcDEmarw`. Base (`app…`) and table (`tbl…`) ids are parsed from it; the view segment is ignored. |
| `TRANSLATION_PROVIDER` | `anthropic` (default), `openai`, or `none` to skip all AI translation. |
| `TRANSLATION_MODEL` | Optional. Defaults to `claude-sonnet-4-6` or `gpt-4o-mini` from the provider. |
| `TRANSLATION_BASE_URL` | Optional. Leave unset for the official API host. Set only for a proxy or OpenAI-compatible gateway. |
| `WORKFLOW_PROFILES_JSON` | JSON object with `translators`, `editors`, and `timing_editors` arrays (see below). |
| `DRIVE_URL` | Parent Google Drive folder URL. Combined media, events, overrides, quotes, and thumbnail review use named subfolders (`Combined Media Files`, `Events`, `Overrides`, `Quotes`, `Thumbnails for approval`). SAVE SOIL end cards are `SaveSoilReel.jpeg` / `SaveSoilVideo.jpeg` in `Overrides/Images`. Example: `https://drive.google.com/drive/folders/1hJZgKn2MwztFzzd7J3rGuh4xCg3su6cg`. |
| `CANVA_URL` | Parent Canva folder URL. Catalog thumbnails use child folders named `Long videos` and `Short videos`. Example: `https://www.canva.com/folder/FAHSXg0enw4`. |

### Required when ingest runs (web session mode)

Ingest uses Playwright with a saved Smartcat browser session (no company API required). Set:

| Secret | Description |
|--------|-------------|
| `SMARTCAT_STORAGE_STATE_JSON` | Full contents of local `smartcat-state.json` (from `python -m catalog_parser --smartcat-login`). |
| `GMAIL_SMTP_USER` | Gmail address used to send alert emails (e.g. your workspace Gmail). |
| `GMAIL_SMTP_APP_PASSWORD` | [Gmail App Password](https://myaccount.google.com/apppasswords) for that account (not your normal login password). |

Alert emails go to the repository variable `NOTIFY_EMAIL`. Also set:

| Secret | Description |
|--------|-------------|
| `HAPPYSCRIBE_API_KEY` | HappyScribe API key used to check the watched library folder for leftover transcriptions (same secret as the publish workflow). Optional; the check is skipped when unset. |

### Optional: Canva thumbnail export during ingest

When package docs include a Canva link, ingest exports that design and uses it as
Airtable **Original Video Thumbnail**.
Uses the **same Canva integration** as publishing (`CANVA_*` secrets →
`credentials/canva-token.json`).

Drive TN templates are **not** used at ingest; they are only used later when
generating the translated thumbnail at publish time.

| Secret | Description |
|--------|-------------|
| `CANVA_CLIENT_ID` | Canva Connect integration client id (shared with publish workflow). |
| `CANVA_CLIENT_SECRET` | Canva Connect integration client secret. |
| `CANVA_TOKEN_JSON` | Full contents of `credentials/canva-token.json`. |
| `CONFIG_SYNC_PAT` | Fine-grained GitHub PAT with **Secrets** and **Variables** Read and write on this repo. After CI refreshes the Canva token, the app updates `CANVA_TOKEN_JSON` automatically. |

If Canva auth is missing or broken when a package has a Canva design link, ingest
**fails** (do not soft-fallback). If auth works but that design is not accessible
to the integration (`permission_denied`), ingest stages a review-queue placeholder
image asking for a **manual Canva download**, then emails the review folder as usual.

### Approved review thumbnails

When ingest finds **no Canva link**, it does **not** write Original Video Thumbnail
to Airtable. If the original-platform thumb still matches the catalog video aspect
ratio, the file is uploaded to the Drive review folder and one review email is
sent for that ingest run. The same review folder is used for **manual Canva**
placeholders when API export is denied for a specific design.

Each daily orchestrator run uploads files from the Drive review folder's **Approved** subfolder into Airtable **Original Video Thumbnail**, then removes them from Drive. When **Video caption translated** is empty, the same run also fills it from the approved image (vision first, Drive TN fallback) using the ingest caption path — skipped when `TRANSLATION_PROVIDER` is `none`, and skipped for manual-Canva placeholders. Approved files must keep the `.review.jpg` filename created by the review queue (for example `Sample Video.review.jpg`).

Uses the same `GOOGLE_SERVICE_ACCOUNT_JSON` secret as the rest of catalog-parser. The review folder is the `Thumbnails for approval` child of `DRIVE_URL`. Optional: `THUMBNAIL_REVIEW_APPROVED_SUBFOLDER`. Review emails need `GMAIL_SMTP_USER`, `GMAIL_SMTP_APP_PASSWORD`, and `NOTIFY_EMAIL` on the orchestrator / ingest job.

### Missing prepared thumbnail on Editing done

When the daily orchestrator detects videos that newly entered **Editing done**
and have an **Original Video Thumbnail** but no matching design/file in the Canva
catalog or Drive override **Thumbnails** folder, it sends one digest email to
`NOTIFY_EMAIL` for all such videos in that run. Each entry includes the title,
translated name, and a Canva design link and/or Drive **TN template** file link
when found (live lookup in the Video Folder package docs / root images).

### Missing prepared thumbnail on publish schedule

When the daily orchestrator schedules tomorrow's video and that record has an
**Original Video Thumbnail** but no matching design/file in the Canva catalog
folder or Drive override **Thumbnails** folder, it emails `NOTIFY_EMAIL` with
the title, translated name, and Canva design / Drive TN template links (live
lookup in the Video Folder when available).
Scheduling still proceeds; the email is informational. Needs the same
`GMAIL_SMTP_*` / `NOTIFY_EMAIL` env as other catalog alerts, plus Canva secrets
when checking the Canva catalog.

Renew locally (either CLI works — same token file):

```powershell
python -m media_publisher --canva-auth
python -m media_publisher --canva-auth-code <authorization-code>
```

### Authorization checks (Smartcat and Canva)

Before each run, the **Check authorization** step validates **Smartcat** (`SMARTCAT_STORAGE_STATE_JSON`). Canva is not probed there: a no-refresh check cannot prove the refresh token still works.

**Canva** (`CANVA_CLIENT_ID`, `CANVA_CLIENT_SECRET`, `CANVA_TOKEN_JSON`) is refreshed if needed and probed at catalog orchestration start. If that fails, the orchestrator exits before Airtable work.

If the Smartcat check fails:

1. An email is sent via Gmail SMTP with renewal instructions.
2. Later workflow steps are skipped (single job; no duplicate Playwright install).

Python dependencies and Playwright Chromium are cached between runs via `.github/actions/setup-python-env` (pip cache + browser cache keyed on `pyproject.toml`).

Renew Smartcat locally:

```powershell
python -m catalog_parser --smartcat-login
```

Then update the `SMARTCAT_STORAGE_STATE_JSON` secret with the new file contents.

Renew Canva locally:

```powershell
python scripts/_canva_auth_interactive.py
```

When `CONFIG_SYNC_PAT` is set, the script updates the `CANVA_TOKEN_JSON` GitHub secret automatically.

Canva refresh tokens are **single-use**. The daily authorization check skips Canva. Catalog orchestration refreshes (if needed) and probes Canva once at startup, then syncs `CANVA_TOKEN_JSON` when `CONFIG_SYNC_PAT` is set. Later steps in the same job must keep the rotated on-disk token — they do not rematerialize `CANVA_TOKEN_JSON` from the job-scoped secret after Restore. Avoid overlapping Canva-using workflows (catalog + publish) that could refresh the same secret concurrently.

Test locally:

```powershell
python scripts/catalog/check_authorization.py --skip-smartcat-if-missing --skip-canva
python scripts/catalog/verify_smartcat_session.py
python -m media_publisher --test-canva
python scripts/catalog/send_notification_email.py --subject "test" --body "test message"
```

(`GMAIL_SMTP_USER` and `GMAIL_SMTP_APP_PASSWORD` must be set in the environment for the email test.)

### HappyScribe watch-folder alert

After a successful authorization check, the daily workflow lists transcriptions in:

`https://www.happyscribe.com/v2/8104266/library/53816432`

If the folder is **not empty**, an email is sent to `NOTIFY_EMAIL` with the item count and titles. The check is non-blocking: an empty folder, missing `HAPPYSCRIBE_API_KEY`, or a HappyScribe API error does not fail the catalog orchestrator.

Override the watched URL with env `HAPPYSCRIBE_WATCH_LIBRARY_URL` (or `--library-url`).

Test locally:

```powershell
python scripts/catalog/check_happyscribe_library.py --skip-if-missing
```

Requires secret `HAPPYSCRIBE_API_KEY` (same as the publish workflow).

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
      "preferred_translation_type": "Reel",
      "preferred_editor": "Editor Name"
    }
  ],
  "editors": [
    {
      "name": "Editor Name",
      "weekly_capacity_reels": 30,
      "preferred_editing_type": "Video"
    }
  ],
  "timing_editors": [
    {
      "name": "Timing Editor Name",
      "weekly_capacity_reels": 30,
      "preferred_timing_type": "Video"
    }
  ]
}
```

Field names must match Airtable **Translator** / **Editor** / **Timing Editor** single-select values. Optional translator `preferred_editor` routes that translator's videos to a specific editor (type preference ignored) and those assignments run first in a workflow pass.

## Repository variables (optional)

Variables are non-secret configuration under **Settings → Secrets and variables → Actions → Variables**.

Optional workflow tuning can be passed as **Variables** (or added to the workflow `env:` block) if you extend the workflow file:

| Variable | Default | Purpose |
|----------|---------|---------|
| `WORKFLOW_REEL_TO_VIDEO_RATIO` | `6` | Target reel:video ratio for ingest. |
| `WORKFLOW_MAX_VIDEO_SECONDS` | `900` | Prefer videos under 15 minutes during ingest. |
| `VIDEO_TYPE` | `Reel` | Default video type when not driven by workflow rules. |
| `SMARTCAT_TARGET_LANGUAGE` | `bg` | Smartcat language for subtitle checks. |

## Google service account setup

1. Create a service account in [Google Cloud Console](https://console.cloud.google.com/) for the same project as your APIs.
2. Enable **Google Sheets API**, **Google Drive API**, and **Google Docs API**.
3. Create a JSON key and store the entire file contents in `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. Share the catalog Google Sheet (`catalog_id` in `workflow_config.json`) with the service account email (`...@....iam.gserviceaccount.com`) as **Viewer**.
5. Share the `DRIVE_URL` parent folder (and any source video folders outside it) with that email as **Editor**. Combined media writes into `Combined Media Files`. Translated SAVE SOIL stills are `SaveSoilReel.jpeg` (Reels/Shorts) and `SaveSoilVideo.jpeg` (Videos) in `Overrides/Images`.

OAuth `credentials.json` / `token.json` are for local development only; CI uses the service account.

## Weekly translation & editing report

The [Reporting workflow](../.github/workflows/reporting.yml) emails a summary every **Monday at 09:00 UTC+3** (06:00 UTC), as part of the daily 06:00 UTC reporting run.

The report covers the **previous calendar week** (Monday 00:00 – Sunday 23:59, UTC+3). It reads `output/workflow/status_history.json` accumulated by daily runs — **no Airtable API calls**.

Daily runs restore the previous `workflow-state` artifact (Airtable backup + status history) before writing a new snapshot. `actions/upload-artifact@v4` strips the common `output/` prefix from uploaded paths, so restore looks under both `backups/…` / `workflow/…` and legacy `output/backups/…` / `output/workflow/…`.

- **Translation** — record entered `2. Translation done` without an Editor (Translator field used for attribution)
- **Editing** — record entered `3. Editing done` without Combined Media File (Editor field used for attribution)

If a record jumps from `1. To do` to `3. Editing done` between daily snapshots, translation and editing are credited separately in the same week.

Recipient: repository variable `NOTIFY_EMAIL`.

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
