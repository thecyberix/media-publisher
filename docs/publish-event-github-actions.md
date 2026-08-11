# Publish event (GitHub Actions)

Manual workflow that announces a Bulgarian **Surya Kriya** programme:

1. Renders the Bulgarian template
2. Appends the event to [`events/data/events.json`](../events/data/events.json) and rebuilds [`events/index.html`](../events/index.html)
3. Posts to the Facebook Page, then comments with the registration link
4. Commits the site files and deploys GitHub Pages

## Dispatch inputs

| Input | Required | Notes |
|-------|----------|--------|
| `city` | yes | Fills `[град]` |
| `country` | yes | Default `България` |
| `date` | yes | `YYYY-MM-DD` |
| `time` | yes | `HH:MM` |
| `registration_link` | yes | Full URL |
| `dry_run` | no | Preview only |
| `skip_facebook` | no | Page update only |

## Secrets

Same Meta secrets as the publish pipeline:

- `META_ACCESS_TOKEN`
- `META_APP_ID`
- `META_APP_SECRET`
- `META_PAGE_ID` (optional if username resolution works)

## Permissions check

Before the first live run:

```bash
python -m media_publisher --check-event-meta
```

Required scopes:

- `pages_manage_posts` — create the feed post
- `pages_manage_engagement` — comment with the registration link

If `pages_manage_engagement` is missing, re-authorize a Page token for a user with the Page **MODERATE** task and update `META_ACCESS_TOKEN`.

## Past events

Events are removed when their start datetime (Europe/Sofia) is in the past:

- **Daily** via [`prune-past-events.yml`](../.github/workflows/prune-past-events.yml) (21:00 UTC ≈ midnight Sofia in summer)
- on every `--publish-event` / **Publish event** run
- manually: `python -m media_publisher --prune-past-events`

When no upcoming events remain, the page shows a centered **Очаквайте скоро!** on the Smartlink cream background.

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
