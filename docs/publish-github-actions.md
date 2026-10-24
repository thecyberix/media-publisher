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

### Ekadashi (12:30 Europe/Sofia)

Suggested schedule: `30 12 * * *`, time zone **Europe/Sofia**. This is a separate job from the 08:00 quotes run.

**URL:** `https://api.github.com/repos/OWNER/REPO/actions/workflows/publish-ekadashi.yml/dispatches`

**Body:**

```json
{
  "ref": "master",
  "inputs": {}
}
```

The workflow publishes the Facebook and Instagram photo, plus a 10-second YouTube video added to the playlist **Екадаши**, the day before the observance and does nothing on other days. A successful post is recorded in `data/ekadashi_publish_state.json` so a second run the same day does not post again.

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
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Drive override thumbnails/videos |
| `CANVA_TOKEN_JSON` | Canva catalog thumbnails |
| `CANVA_CLIENT_ID` / `CANVA_CLIENT_SECRET` | Canva token refresh in CI |
| `CONFIG_SYNC_PAT` | Persist Canva/YouTube tokens and daily-playlist slots (Secrets + Variables write) |
| `AIRTABLE_*`, `HAPPYSCRIBE_*`, `YOUTUBE_*`, `META_*` | Catalog fetch and platform publish |

Facebook quote photos use the Graph API (same as before Playwright). Development-mode Graph posts may be visible only to app roles until App Review / Live.

**Local Playwright (while Development mode limits Graph visibility)**

- Quotes (Facebook only): `python scripts/schedule_prepared_quotes_facebook.py --year 2026 --month 10`
- Events (Facebook only): `python scripts/schedule_event_facebook.py --event-type surya_kriya …`
- Ekadashi (Facebook only): `python scripts/schedule_ekadashi_facebook.py --year 2026 --month 10`. Instagram is published by the `publish-ekadashi` workflow.

Ekadashi dates come from the public Isha Google Calendar ICS (`ishacalendar@gmail.com`). The workflow publishes **Facebook and Instagram**, and a 10-second **YouTube** video on the **Екадаши** playlist, one day before at **12:30 Europe/Sofia**. The local Playwright scheduler posts **Facebook only**. Captions are hard-coded from 2025 Page posts (same ordinal slot). Images are pages of the `Ekadashi {year}` design in the `Ekadashi` folder under `CANVA_URL`.

### Instagram keyword replies

`config/playlists.json` `publish_priority` lists video playlists to schedule first. Airtable status still comes first: Synchronization done is scheduled before Editing done, and Editing done before Translation done. Inside one status, names earlier in `publish_priority` are scheduled sooner; videos on none of those playlists wait behind them. Inner Engineering and Miracle of Mind videos ask people to comment a keyword. `config/playlists.json` holds that keyword, the public reply, and the DM next to the original comment. The checker only looks at videos on the watch list in `data/instagram_comment_responses.json`. That list is filled from `output/instagram-end-card-comments.json` (videos whose comment was posted or already present) and each later Instagram publish adds its own video.

```powershell
python scripts/respond_instagram_playlist_comments.py
python scripts/respond_instagram_playlist_comments.py --apply
```

The first command only lists matches. `--apply` sends the reply and the DM. The token needs `instagram_manage_comments` and `instagram_manage_messages`. DMs use Instagram private replies and only go out within 7 days of the comment; older comments still get the public reply.

Metricool sends these replies. The script remains for a manual run.

Video and quote publish runs refresh Canva (if needed), probe the API once at startup, and sync `CANVA_TOKEN_JSON` / playlist slots when `CONFIG_SYNC_PAT` is set. Canva auth failures abort the run. Catalog YouTube uploads are added to the playlists named on the Airtable record. Quotes and Ekadashi posts use the playlist titles in `config/playlists.json`.

When Original Video Thumbnail is set but Drive override and Canva catalog have no matching translated thumbnail, publish continues without one and emails `NOTIFY_EMAIL`. Generate the thumbnail offline with `scripts/_generate_thumbnails_from_original.py`.

## Repository variables

| Variable | Used for |
|----------|----------|
| `DRIVE_URL` | Parent Google Drive folder (`Automated Workflow`); publish uses `Overrides`, `Quotes`, and `Thumbnails for approval`. Quote year workbooks live in `Quotes` and are named from `year_workbook_pattern` in `config/quotes_sources.json`. |
| `CANVA_URL` | Parent Canva folder. Catalog thumbnails use `Thumbnails/Long videos` and `Thumbnails/Short videos`. Ekadashi posts use the sibling folder `Ekadashi`. |
| `HAPPYSCRIBE_URL` | Primary HappyScribe library for catalog video lookup. Required. If it has `Short videos` / `Long videos` children, only the folder matching the video type is searched. |
| `HAPPYSCRIBE_REVIEW_URL` | Optional parent HappyScribe library. Publish also searches the type-matching child (`Short videos` or `Long videos`). Unset: skip that fallback. |
| `YOUTUBE_CHANNEL_HANDLE` | YouTube handle. Required for publish. |
| `YOUTUBE_DAILY_PLAYLIST_JSON` | Daily Smartlink playlist id plus slot state (`playlist_id`, `quote`, `reel`, `lau`). Unset: skip daily playlist updates. |
| `META_PAGE_USERNAME` | Facebook Page username. Required for Meta publish. |
| `META_INSTAGRAM_USERNAME` | Instagram username. Required for Meta publish. |
| `TARGET_LANGUAGE` | Language key in `config/languages.json`. Required. |
| `SMARTLINK_URL` | Metricool Smartlink for video captions. Required for publish. |
| `ENGLISH_QUOTES_URL` | English source quotes spreadsheet. Required for `--prepare-quote-texts`. |
| `NOTIFY_EMAIL` | Catalog / auth / thumbnail alerts, quote-text and generated-quotes Drive sync mail. Unset: skip those emails. |
| `PUBLISH_JSON` | Timezone and local hours: `{"timezone":"Europe/Sofia","quotes_hour":8,"videos_hour":18}`. Required. |

Also uses secrets `GMAIL_SMTP_USER` and `GMAIL_SMTP_APP_PASSWORD` when sending email. If those or `NOTIFY_EMAIL` are unset, email is skipped.

## Headers (all cron-job.org publish jobs)

| Name | Value |
|------|-------|
| `Authorization` | `Bearer YOUR_TOKEN` |
| `Accept` | `application/vnd.github+json` |
| `X-GitHub-Api-Version` | `2022-11-28` |
| `Content-Type` | `application/json` |
