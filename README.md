# media-publisher

Extract publishing metadata from **Airtable**, **HappyScribe**, and **Canva**, then publish videos to **YouTube**, **Facebook**, and **Instagram**. Also includes the **catalog-parser** ingest and daily translation workflow (Google Sheets → Smartcat/Drive → Airtable).

## Pipeline

```
Google Sheet ──► catalog-parser ingest ──► Airtable ──► media-publisher ──► YouTube / Facebook / Instagram
                      Smartcat / Drive / Canva              HappyScribe / Canva
```

## Setup (local)

```powershell
cd C:\Users\GeorgiUzunov\Projects\media-publisher
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
copy .env.example .env
```

Fill in `.env` with API credentials and place OAuth JSON files under `credentials/` (see `.env.example`).

## Usage

```powershell
python -m media_publisher --help
python -m media_publisher --check-config
python -m media_publisher                  # publish today's catalog videos
python -m media_publisher --quotes           # publish today's quote posts

python -m catalog_parser                   # daily translation workflow (default)
python -m catalog_parser ingest            # parse Google Sheet → enrich → Airtable
python -m catalog_parser --smartcat-login  # renew Smartcat browser session
python -m catalog_parser --canva-auth      # Canva OAuth for ingest thumbnails
```

See `docs/catalog-github-actions.md` for catalog workflow secrets and schedule. See `docs/publish-github-actions.md` for publish triggers (cron-job.org, API).

## GitHub Actions (scheduled publishing)

Workflows live under `.github/workflows/`:

| Workflow | Purpose |
|----------|---------|
| `ci.yml` | Runs unit tests on push/PR |
| `publish.yml` | Manual + external cron publishing (`workflow_dispatch`) |
| `catalog-daily-workflow.yml` | Daily ingest, editor assignment, media mixing, Airtable sync |
| `reporting.yml` | Snapshots, weekly email, monthly KPIs, prune past events |

### Repository secrets

Set these under **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Purpose |
|--------|---------|
| `AIRTABLE_TOKEN` | Airtable personal access token |
| `TRANSLATION_API_KEY` | Anthropic or OpenAI key (matches `TRANSLATION_PROVIDER`) |
| `AIRTABLE_VIEW` | Optional — leave unset for publishing/audits (uses full table) |
| `HAPPYSCRIBE_API_KEY` | HappyScribe API key |
| `CANVA_CLIENT_ID` | Canva OAuth client ID |
| `CANVA_CLIENT_SECRET` | Canva OAuth client secret |
| `CANVA_TOKEN_JSON` | Full contents of `credentials/canva-token.json` |
| `CONFIG_SYNC_PAT` | Fine-grained GitHub PAT with **Secrets** and **Variables** Read and write on this repo (syncs Canva/YouTube tokens and `YOUTUBE_DAILY_PLAYLIST_SLOTS_JSON`) |
| `YOUTUBE_CLIENT_SECRETS_JSON` | Full contents of `credentials/youtube-client.json` |
| `YOUTUBE_TOKEN_JSON` | Full contents of `credentials/youtube-token.json` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service account key JSON (shared with catalog-parser; written to `credentials/google-sheets-service-account.json` for channel reports) |
| `META_APP_ID` | Meta app ID |
| `META_APP_SECRET` | Meta app secret |
| `META_ACCESS_TOKEN` | Long-lived Meta page access token |

### Repository variables

Set under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Example |
|----------|---------|
| `AIRTABLE_URL` | `https://airtable.com/app.../tbl...` |
| `CANVA_URL` | `https://www.canva.com/folder/FAHSXg0enw4` |
| `TRANSLATION_PROVIDER` | `anthropic` (or `none` to skip AI translation) |
| `HAPPYSCRIBE_URL` | `https://www.happyscribe.com/v2/.../library/...` |
| `PUBLISH_TIMEZONE` | `Europe/Sofia` |
| `VIDEOS_PUBLISH_HOUR` | `18` |
| `QUOTES_PUBLISH_HOUR` | `8` |
| `YOUTUBE_PLAYLIST_ID` | `PLpP5d0BDr0xaGn6QSPyQjG6GhK-dcm3Lm` |
| `YOUTUBE_DAILY_PLAYLIST_ID` | `PLKM1FUqZWv28` |

### How CI credentials work

At startup, `load_settings()` reads environment variables (injected by GitHub Actions from secrets) and, when `*_JSON` variables are set, writes them to `credentials/` before the app runs. Locally, if those `*_JSON` variables are unset, existing files in `credentials/` are used as before.

**Sharing Google credentials:** Use one `GOOGLE_SERVICE_ACCOUNT_JSON` org/repo secret for catalog ingest, channel reports, and Drive access.

**Canva (shared):** Publishing and catalog ingest use the same `CANVA_CLIENT_ID` / `CANVA_TOKEN_JSON` → `credentials/canva-token.json`. Set `CONFIG_SYNC_PAT` locally (and in GitHub Actions secrets) so Canva/YouTube token refreshes and daily-playlist slot updates are written back automatically.

### Reporting (snapshots, weekly work, monthly views, past events)

| Workflow | Purpose |
|----------|---------|
| `reporting.yml` | Follower snapshots, Monday work-report email, monthly **Views Actual**, prune past events |

Scheduled daily at **06:00 UTC**: snapshots and prune past events every day; weekly email on Mondays; channel KPIs on the 2nd. Manual run: **Actions → Reporting → Run workflow** (task `auto`, or pick `snapshots` / `weekly-work` / `channel-report` / `prune-past-events`).

Local commands:

```powershell
python -m media_publisher --dry-run-channel-report
python -m media_publisher --update-channel-report
python scripts/update_channel_report.py --dry-run
python -m media_publisher --update-channel-report --channel-report-all-months
python -m media_publisher --update-channel-report --channel-report-month 2026-02
```

Requires `GOOGLE_SERVICE_ACCOUNT_JSON`, YouTube OAuth token with `yt-analytics.readonly`, and Meta page token with `read_insights` + `instagram_manage_insights`. Mapping: `config/channel_report_bulgarian.json`.

### Manual publish run

In GitHub: **Actions → Publish videos and quotes → Run workflow**.

| Input | Default | Use |
|-------|---------|-----|
| **mode** | `all` | `all`, `videos`, or `quotes` |
| **YouTube / Facebook / Instagram** | all on | Uncheck a platform to skip it (retry-safe) |
| **timing** | `standard` | How to publish (see below) |

**Timing modes**

| Mode | Behavior |
|------|----------|
| **standard** (default) | Production cadence: Instagram today immediately; YouTube and Facebook scheduled for tomorrow so they can be reviewed. Same as the nightly cron jobs. |
| **immediate** | Publish everything due today on the selected platforms right now. |
| **scheduled** | Schedule YouTube and Facebook for the next publish slot (today’s hour if it has not passed, otherwise tomorrow). Instagram is skipped. Facebook goes live as `SCHEDULED` (public at that time), not as a private draft. |

Long-form catalog rows with Type=Video still skip Instagram even when the Instagram checkbox is on.

To trigger the same way as cron-job.org (API / script), see `docs/publish-github-actions.md`.

## Project layout

```
src/media_publisher/     publishing CLI, sources, publishers
src/catalog_parser/      catalog ingest + daily workflow orchestrator
scripts/catalog/         catalog maintenance and CI helper scripts
config/workflow_config.example.json
.github/workflows/
  ci.yml
  publish.yml
  catalog-daily-workflow.yml
  reporting.yml
```

## API credentials (local)

| Service | What you need |
|---------|----------------|
| Airtable | Personal access token, base ID, table name |
| HappyScribe | API key |
| Canva | OAuth app (client ID + secret) + token file |
| YouTube | Google Cloud project, YouTube Data API, OAuth desktop client |
| Facebook / Instagram | Meta app, page access token, Instagram business account ID |
