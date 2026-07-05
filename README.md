# media-publisher

Extract publishing metadata from **Airtable**, **HappyScribe**, and **Canva**, then publish videos to **YouTube**, **Facebook**, and **Instagram** via their APIs.

## Pipeline

```
Airtable  ──┐
HappyScribe ├── collect & normalize ──► publish ──► YouTube
Canva       ──┘                         │           Facebook
                                        └──────────► Instagram
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
```

## GitHub Actions (scheduled publishing)

Two workflows live under `.github/workflows/`:

| Workflow | Purpose |
|----------|---------|
| `ci.yml` | Runs unit tests on push/PR |
| `publish.yml` | Scheduled + manual publishing |

### Repository secrets

Set these under **Settings → Secrets and variables → Actions → Secrets**:

| Secret | Purpose |
|--------|---------|
| `AIRTABLE_TOKEN` | Airtable personal access token |
| `AIRTABLE_BASE_ID` | Airtable base ID |
| `AIRTABLE_TABLE_NAME` | Airtable table name |
| `HAPPYSCRIBE_API_KEY` | HappyScribe API key |
| `HAPPYSCRIBE_LIBRARY_URL` | HappyScribe library URL (or use org + folder IDs below) |
| `HAPPYSCRIBE_ORGANIZATION_ID` | Optional if `HAPPYSCRIBE_LIBRARY_URL` is set |
| `HAPPYSCRIBE_FOLDER_ID` | Optional if `HAPPYSCRIBE_LIBRARY_URL` is set |
| `CANVA_CLIENT_ID` | Canva OAuth client ID |
| `CANVA_CLIENT_SECRET` | Canva OAuth client secret |
| `CANVA_TOKEN_JSON` | Full contents of `credentials/canva-token.json` |
| `YOUTUBE_CLIENT_SECRETS_JSON` | Full contents of `credentials/youtube-client.json` |
| `YOUTUBE_TOKEN_JSON` | Full contents of `credentials/youtube-token.json` |
| `META_APP_ID` | Meta app ID |
| `META_APP_SECRET` | Meta app secret |
| `META_ACCESS_TOKEN` | Long-lived Meta page access token |
| `META_PAGE_ID` | Optional — resolved from username if omitted |
| `META_INSTAGRAM_ACCOUNT_ID` | Optional — resolved from page if omitted |

### Repository variables

Set under **Settings → Secrets and variables → Actions → Variables**:

| Variable | Example |
|----------|---------|
| `PUBLISH_TIMEZONE` | `Europe/Sofia` |
| `PUBLISH_HOUR` | `18` |
| `QUOTES_PUBLISH_TIMEZONE` | `Europe/Sofia` |
| `QUOTES_PUBLISH_HOUR` | `8` |

### How CI credentials work

At startup, `load_settings()` reads environment variables (injected by GitHub Actions from secrets) and, when `*_JSON` variables are set, writes them to `credentials/` before the app runs. Locally, if those `*_JSON` variables are unset, existing files in `credentials/` are used as before.

### Manual publish run

In GitHub: **Actions → Publish → Run workflow**. Choose `videos`, `quotes`, or `all`, and enable **private** for a safe test run.

## Project layout

```
src/media_publisher/
  __main__.py          CLI entry point
  config.py            env / settings loading
  runtime_env.py       materialize credential files from env (CI)
  models.py            shared record types
  sources/             data extraction
  publishers/          platform publishing
.github/workflows/
  ci.yml               unit tests
  publish.yml          scheduled publishing
```

## API credentials (local)

| Service | What you need |
|---------|----------------|
| Airtable | Personal access token, base ID, table name |
| HappyScribe | API key |
| Canva | OAuth app (client ID + secret) + token file |
| YouTube | Google Cloud project, YouTube Data API, OAuth desktop client |
| Facebook / Instagram | Meta app, page access token, Instagram business account ID |
