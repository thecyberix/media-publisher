# Publish event (GitHub Actions)

Manual workflow that announces a Bulgarian **Surya Kriya**, **Bhuta Shuddhi**, or **Yogasanas** programme:

1. Renders the Bulgarian template for the selected programme
2. Appends the event to [`events/data/events.json`](../events/data/events.json) and rebuilds [`events/index.html`](../events/index.html)
3. Posts a photo + caption to the Facebook Page (template text, including registration link)
4. Commits the site files and deploys GitHub Pages

## Dispatch inputs

| Input | Required | Notes |
|-------|----------|--------|
| `event_type` | yes | `Surya Kriya`, `Bhuta Shuddhi`, or `Yogasanas` |
| `city` | yes | Fills `[град]` |
| `country` | no | Blank uses the country from `config/languages.json` for `TARGET_LANGUAGE` |
| `date` | yes | `YYYY-MM-DD` |
| `time` | yes | `HH:MM` |
| `registration_link` | yes | Full URL |
| `image_id` | no | Drive file id, filename (`1.jpg`), or number (`1`); blank rotates images |
| `dry_run` | no | Preview only |
| `skip_facebook` | no | Page update only |

## Secrets

Same Meta secrets as the publish pipeline:

- `META_ACCESS_TOKEN`
- `META_APP_ID`
- `META_APP_SECRET`
- `GOOGLE_SERVICE_ACCOUNT_JSON` — loads the Hatha message template and Facebook images from Drive
- `DRIVE_URL` — parent Drive folder; events use the `Events` subfolder

Programme copy (quote, body, benefits, YouTube link) is read from the Hatha WhatsApp template in the `Events` folder under `DRIVE_URL`. The YouTube URL comes only from that document (language cell, else English cell).

Facebook images live in the same folder:

- `Surya Kriya/` — images for Surya Kriya
- `Bhuta Shuddhi/` — images for Bhuta Shuddhi
- `Yogasanas/` — images for Yogasanas

When `image_id` is omitted, the workflow picks the next unused image in that subfolder (by filename), then wraps around after all images have been used. Explicit `image_id` choices count as used for later defaults. Usage history is stored in [`events/data/facebook-image-rotation.json`](../events/data/facebook-image-rotation.json) and each event also records `facebook_image_id`.

## Permissions check

Before the first live run:

```bash
python -m media_publisher --check-event-meta
```

Required scopes:

- `pages_manage_posts` — create the photo post

If `pages_manage_posts` is missing, re-authorize a Page token for a user with the Page **CREATE CONTENT** task and update `META_ACCESS_TOKEN`.

## Past events

Events are removed when their start datetime (Europe/Sofia) is in the past:

- **Daily** via the [Reporting workflow](../.github/workflows/reporting.yml) at 06:00 UTC
- on every `--publish-event` / **Publish event** run
- manually: `python -m media_publisher --prune-past-events`

When no upcoming events remain, the page shows the Sadhguru portrait header and **Очаквайте скоро!** on the Smartlink cream background.

## Local dry run

```bash
python -m media_publisher --publish-event \
  --city "София" \
  --date 2026-09-15 \
  --time 18:00 \
  --registration-link "https://example.com/register" \
  --dry-run
```

## GitHub Pages

Enable Pages with **GitHub Actions** as the source (recommended). The workflow uploads the `events/` folder via `actions/deploy-pages`.
