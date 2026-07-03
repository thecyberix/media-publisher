# media-publisher

Extract publishing metadata from **Airtable**, **HappyScribe**, and **Canva**, then publish videos to **YouTube**, **Facebook**, and **Instagram** via their APIs.

## Pipeline (planned)

```
Airtable  ──┐
HappyScribe ├── collect & normalize ──► publish ──► YouTube
Canva       ──┘                         │           Facebook
                                        └──────────► Instagram
```

## Setup

```powershell
cd C:\Users\GeorgiUzunov\Projects\media-publisher
python -m venv .venv
.\.venv\Scripts\activate
pip install -e .
copy .env.example .env
```

Fill in `.env` with API credentials for each service you plan to use.

## Usage

```powershell
python -m media_publisher --help
```

## Project layout

```
src/media_publisher/
  __main__.py          CLI entry point
  config.py            env / settings loading
  models.py            shared record types
  sources/             data extraction
    airtable.py
    happyscribe.py
    canva.py
  publishers/          platform publishing
    youtube.py
    facebook.py
    instagram.py
```

## API credentials

| Service | What you need |
|---------|----------------|
| Airtable | Personal access token, base ID, table name |
| HappyScribe | API key |
| Canva | OAuth app (client ID + secret) |
| YouTube | Google Cloud project, YouTube Data API, OAuth desktop client |
| Facebook / Instagram | Meta app, page access token, Instagram business account ID |

## Status

Early scaffold — integrations are stubbed and ready to be implemented.
