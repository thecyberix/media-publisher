# media-publisher

Extract publishing metadata from **Airtable**, **HappyScribe**, and **Canva**, then publish videos to **YouTube**, **Facebook**, and **Instagram**. Also includes the **catalog-parser** ingest and daily translation workflow (Google Sheets → Smartcat/Drive → Airtable).

## Pipeline

```
Google Sheet ──► catalog-parser ingest ──► Airtable ──► media-publisher ──► YouTube / Facebook / Instagram
                      Smartcat / Drive / Canva              HappyScribe / Canva
```

## Setup (local)

```powershell
cd $env:USERPROFILE\Projects\media-publisher
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
| `publish-event.yml` | Announce a programme and update the events GitHub Pages site |

### Repository secrets

Set under **Settings → Secrets and variables → Actions → Secrets**. These are the secrets this repo currently uses:

| Secret | Purpose |
|--------|---------|
| `AIRTABLE_TOKEN` | Airtable personal access token |
| `CANVA_CLIENT_ID` | Canva OAuth client ID |
| `CANVA_CLIENT_SECRET` | Canva OAuth client secret |
| `CANVA_TOKEN_JSON` | Full contents of `credentials/canva-token.json` |
| `CONFIG_SYNC_PAT` | Fine-grained PAT with **Actions secrets** and **Actions variables** Read and write (syncs Canva/YouTube tokens and `YOUTUBE_DAILY_PLAYLIST_JSON`) |
| `GMAIL_SMTP_USER` | Gmail address for workflow alert mail |
| `GMAIL_SMTP_APP_PASSWORD` | Gmail app password (not the account login password) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Full service account key JSON (Sheets, Drive, channel reports) |
| `HAPPYSCRIBE_API_KEY` | HappyScribe API key |
| `META_ACCESS_TOKEN` | Long-lived Meta page access token |
| `META_APP_ID` | Meta app ID |
| `META_APP_SECRET` | Meta app secret |
| `SMARTCAT_STORAGE_STATE_JSON` | Full contents of `smartcat-state.json` (Playwright session) |
| `TRANSLATION_API_KEY` | Anthropic or OpenAI key (matches `TRANSLATION_PROVIDER`) |
| `YOUTUBE_CLIENT_SECRETS_JSON` | Full contents of `credentials/youtube-client.json` |
| `YOUTUBE_TOKEN_JSON` | Full contents of `credentials/youtube-token.json` |

`WORKFLOW_PROFILES_JSON` may also be stored as a secret; this repo keeps it as a variable.

### Repository variables

Set under **Settings → Secrets and variables → Actions → Variables**. These are the variables this repo currently uses:

| Variable | Purpose |
|----------|---------|
| `AIRTABLE_URL` | Live catalog table URL (`app…` / `tbl…` are parsed; the view segment is ignored) |
| `CANVA_URL` | Parent Canva folder; catalog thumbs use child folders `Long videos` and `Short videos` |
| `DRIVE_URL` | Parent Drive folder (`Combined Media Files`, `Events`, `Overrides`, `Quotes`, `Thumbnails for approval`) |
| `GENERATED_QUOTES_NOTIFY_EMAIL` | Recipients for generated-quotes Drive sync mail (comma-separated) |
| `HAPPYSCRIBE_URL` | HappyScribe library URL. If it contains `Short videos` / `Long videos` children, publish uses the folder that matches the video type. |
| `META_INSTAGRAM_USERNAME` | Instagram username for the linked business account |
| `META_PAGE_USERNAME` | Facebook Page username |
| `NOTIFY_EMAIL` | Recipients for catalog / auth / workflow failure mail |
| `PUBLISH_JSON` | Publish schedule, e.g. `{"timezone":"Europe/Sofia","quotes_hour":8,"videos_hour":18}` |
| `SMARTLINK_URL` | Metricool Smartlink URL used in captions |
| `TARGET_LANGUAGE` | Language name for channel-report sheet tab and related copy (e.g. `Bulgarian`) |
| `TRANSLATED_QUOTES_URL` | Google Sheet of daily translated quotes |
| `TRANSLATION_PROVIDER` | `anthropic` or `openai` (case-insensitive); `none` skips AI translation |
| `WORKFLOW_PROFILES_JSON` | JSON with `translators`, `editors`, and `timing_editors` arrays |
| `YOUTUBE_CHANNEL_HANDLE` | YouTube channel handle (no `@`) |
| `YOUTUBE_DAILY_PLAYLIST_JSON` | Daily playlist id plus quote/reel/lau slot video ids (and optional `pending`) |
| `YOUTUBE_PLAYLIST_ID` | Archive playlist id for published catalog videos and quotes |

Optional (read by workflows when set):

| Variable | Purpose |
|----------|---------|
| `HAPPYSCRIBE_REVIEW_URL` | Parent HappyScribe library. Leftover-folder email watches `Short videos` and `Long videos`. Publish also searches the type-matching child. Unset: skip the email check and the fallback search. |
| `TRANSLATION_MODEL` | Override the default model (`claude-sonnet-4-6` for Anthropic, `gpt-4o-mini` for OpenAI) |

### Add a new language

The running language is `TARGET_LANGUAGE` (GitHub variable and local `.env`). It must match a top-level key in `config/languages.json` (this repo uses `Bulgarian`). Copy that object and fill in the new language.

Required on every language:

- `alias` — short code used for Smartcat, HappyScribe filename suffixes, and HTML `lang` (for example `bg`)
- `country` — default country for event announcements
- `months` — twelve month names in that language
- `date_year_suffix` — optional text after the year in dates (Bulgarian uses ` г.`)
- `events` — programme page and Facebook copy (`program_word`, headings, registration CTA, empty state, and so on)
- `ingest` — Smartcat language id, extra aliases, quotation marks, letter regex, and title-case small words
- `publish` — display name, hashtags, YouTube title suffix, tags, and “learn more” label

Ingest mix of Reels vs long videos is also in `config/workflow_config.json`:

| Field | Meaning |
|-------|---------|
| `target_reel_to_video_ratio` | Prefer Reels until there are this many Reels per Video (this repo uses `6`) |
| `max_video_seconds` | Skip catalog Videos longer than this during ingest (this repo uses `900`, 15 minutes) |

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

Requires `GOOGLE_SERVICE_ACCOUNT_JSON`, YouTube OAuth token with `yt-analytics.readonly`, and Meta page token with `read_insights` + `instagram_manage_insights`. Mapping: `config/channel_report.json`. The Sheet tab title is the `TARGET_LANGUAGE` name (for example `Bulgarian`).

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
config/workflow_config.json
config/workflow_config.example.json
.github/workflows/
  ci.yml
  publish.yml
  catalog-daily-workflow.yml
  reporting.yml
  publish-event.yml
.github/workflow-backups/   inactive workflow YAML (not run)
```

## API credentials (local)

| Service | What you need |
|---------|----------------|
| Airtable | Personal access token, base ID, table name |
| HappyScribe | API key |
| Canva | OAuth app (client ID + secret) + token file |
| YouTube | Google Cloud project, YouTube Data API, OAuth desktop client |
| Facebook / Instagram | Meta app, page access token, Instagram business account ID |
