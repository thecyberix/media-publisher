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
| `country` | yes | Default `България` |
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
- `META_PAGE_ID` (optional if username resolution works)
- `GOOGLE_SERVICE_ACCOUNT_JSON` — loads the Hatha message template and Facebook images from Drive

Programme copy (quote, body, benefits, YouTube link) is read from the Hatha WhatsApp template in [this Drive folder](https://drive.google.com/drive/folders/1ENCdaCLVYdCgXSq0X3Fg5pj2f2L3hlI6) (`NEW Hatha WA Message Template ENG.docx`). Bulgarian cells are used, including the YouTube hyperlink on the video title.

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
