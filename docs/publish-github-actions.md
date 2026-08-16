# GitHub Actions — publish workflow

The [publish workflow](../.github/workflows/publish.yml) runs catalog video and daily quote publishing. Like the catalog daily workflow, it is triggered via **`workflow_dispatch`** (manual UI, local script, or [cron-job.org](https://cron-job.org)).

Default **timing** is **standard** for both the GitHub UI and cron-job.org: Instagram today immediately; YouTube/Facebook tomorrow.

## Manual run (GitHub UI)

**Actions → Publish videos and quotes → Run workflow**

| Input | Default | Use |
|-------|---------|-----|
| **mode** | `all` | `all`, `videos`, or `quotes` |
| **YouTube / Facebook / Instagram** | all on | Uncheck a platform to skip it (retry-safe) |
| **timing** | `standard` | How to publish (see below) |

### Timing modes

| Mode | Behavior |
|------|----------|
| **standard** (default) | Production cadence: Instagram today immediately; YouTube and Facebook scheduled for tomorrow so they can be reviewed. Same as the nightly cron jobs. |
| **immediate** | Publish everything due today on the selected platforms right now. |
| **scheduled** | Schedule YouTube and Facebook for the next publish slot (today’s hour if it has not passed, otherwise tomorrow). Instagram is skipped. Facebook goes live as `SCHEDULED` (public at that time), not as a private draft. |

Long-form catalog rows with Type=Video still skip Instagram even when the Instagram checkbox is on.

## Trigger via API (same as cron-job.org)

Use the same PAT as the catalog cron job (**Actions: Read and write** on this repo). Branch: **`master`**.

### Scheduled test run (PowerShell)

```powershell
$headers = @{
  Authorization = "Bearer YOUR_TOKEN"
  Accept        = "application/vnd.github+json"
  "X-GitHub-Api-Version" = "2022-11-28"
}
$body = @{
  ref = "master"
  inputs = @{
    mode   = "all"
    timing = "scheduled"
  }
} | ConvertTo-Json

Invoke-RestMethod `
  -Method POST `
  -Uri "https://api.github.com/repos/OWNER/REPO/actions/workflows/publish.yml/dispatches" `
  -Headers $headers `
  -Body $body `
  -ContentType "application/json"
```

Expected: HTTP **204**. A new run appears under **Actions → Publish videos and quotes** within seconds.

### Local helper script

```powershell
$env:GITHUB_DISPATCH_TOKEN = "YOUR_TOKEN"
python scripts/trigger_github_workflow_dispatch.py publish.yml --timing scheduled --mode all
```

### Preset runner (dispatch + wait + failed logs)

Copy `config/github_workflows.example.json` to `config/github_workflows.json` if you want local overrides (repo, ref, presets). Leave `repository` empty to use `GITHUB_REPOSITORY` or git `origin`. The token stays in `GITHUB_DISPATCH_TOKEN`, not in the JSON file.

```powershell
$env:GITHUB_DISPATCH_TOKEN = "YOUR_TOKEN"
python scripts/run_github_workflow.py --list
python scripts/run_github_workflow.py publish-private-videos
```

On failure, the script prints the GitHub run URL and the tail of failed job logs.

## cron-job.org jobs (production schedule)

Create separate cron jobs per content type. Reuse the same GitHub token and headers as [catalog-github-actions.md](catalog-github-actions.md).

### Catalog videos (~18:00 Europe/Sofia)

Suggested schedule: `0 18 * * *`, time zone **Europe/Sofia**.

**URL:** `https://api.github.com/repos/OWNER/REPO/actions/workflows/publish.yml/dispatches`

**Body:**

```json
{
  "ref": "master",
  "inputs": {
    "mode": "videos",
    "timing": "standard"
  }
}
```

### Daily quotes (~08:00 Europe/Sofia)

Suggested schedule: `0 8 * * *`, time zone **Europe/Sofia**.

**Body:**

```json
{
  "ref": "master",
  "inputs": {
    "mode": "quotes",
    "timing": "standard"
  }
}
```

`timing` defaults to `standard` if omitted. YouTube, Facebook, and Instagram default to on.

### One-off scheduled test from cron-job.org

Use **Run now** on a throwaway cron job (or a dedicated “publish scheduled test” job) with:

```json
{
  "ref": "master",
  "inputs": {
    "mode": "all",
    "timing": "scheduled"
  }
}
```

Boolean workflow inputs must be the strings `"true"` or `"false"` in JSON.

## Required GitHub secrets

Same as the catalog workflow where noted:

| Secret | Used for |
|--------|----------|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Drive override thumbnails/videos, TN template download |
| `CANVA_TOKEN_JSON` | Canva catalog thumbnails (reels/videos without TN caption) |
| `CANVA_CLIENT_ID` / `CANVA_CLIENT_SECRET` | Canva token refresh in CI |
| `CONFIG_SYNC_PAT` | Persist Canva/YouTube tokens and daily-playlist slots (Secrets + Variables write) |
| `AIRTABLE_*`, `HAPPYSCRIBE_*`, `YOUTUBE_*`, `META_*` | Catalog fetch and platform publish |

Video and quote publish runs refresh Canva (if needed), probe the API once at startup, and sync `CANVA_TOKEN_JSON` / playlist slots when `CONFIG_SYNC_PAT` is set. Canva auth failures abort the run instead of falling through to TN generation.

The publish workflow installs the `thumbnails` extra (`psd-tools`) so TN render can run when Drive override and Canva catalog lookup fail for non-auth reasons.

## Repository variables

| Variable | Used for |
|----------|----------|
| `DRIVE_URL` | Parent Google Drive folder (`Automated Workflow`); publish uses `Overrides`, `Quotes`, and `Thumbnails for approval` |
| `CANVA_URL` | Parent Canva folder; catalog thumbnails use `Long videos` and `Short videos` |
| `YOUTUBE_CHANNEL_HANDLE` | YouTube handle. Required for publish. |
| `YOUTUBE_PLAYLIST_ID` | Channel playlist for published catalog videos and quotes. Unset: skip playlist add. |
| `YOUTUBE_DAILY_PLAYLIST_JSON` | Daily Smartlink playlist id plus slot state (`playlist_id`, `quote`, `reel`, `lau`). Unset: skip daily playlist updates. |
| `META_PAGE_USERNAME` | Facebook Page username. Required for Meta publish. |
| `META_INSTAGRAM_USERNAME` | Instagram username. Required for Meta publish. |
| `TARGET_LANGUAGE` | Language key in `config/languages.json`. Required. |
| `SMARTLINK_URL` | Metricool Smartlink for video captions. Required for publish. |
| `TRANSLATED_QUOTES_URL` | Google Sheet of daily translated quotes. Required for quote publish. |
| `NOTIFY_EMAIL` | Catalog / auth / thumbnail alerts. Unset: skip those emails. |
| `GENERATED_QUOTES_NOTIFY_EMAIL` | Generated-quotes Drive sync emails. Unset: skip those emails. |
| `PUBLISH_TIMEZONE` | IANA timezone for scheduling. Required. |
| `QUOTES_PUBLISH_HOUR` | Local hour for quote slots. Required. |
| `VIDEOS_PUBLISH_HOUR` | Local hour for video slots. Required. |

Also uses secrets `GMAIL_SMTP_USER` and `GMAIL_SMTP_APP_PASSWORD` when sending email. If those or `NOTIFY_EMAIL` are unset, email is skipped.

## Headers (all cron-job.org publish jobs)

| Name | Value |
|------|-------|
| `Authorization` | `Bearer YOUR_TOKEN` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |
