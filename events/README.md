# Sadhguru Bulgaria events (GitHub Pages)

Public listing of Isha programme announcements for Bulgaria. v1 supports **Surya Kriya** (Bulgarian).

Styled to match the Metricool SmartLink:
https://t-sml.mtrbio.com/public/smartlink/sadhguru-bulgarian
(cream background `#F9F4F3`, Merriweather, dark brown text, pill CTAs).

## Enable GitHub Pages

1. Repo **Settings → Pages**
2. Prefer **GitHub Actions** as the source (the `Publish event` workflow deploys this folder), **or**
3. Deploy from branch with folder `/events`

Site files:

- `index.html` — rendered listing
- `data/events.json` — source of truth (updated by the workflow)

## Publish an event

Run the **Publish event** GitHub Action (`publish-event.yml`) with:

| Input | Example |
|-------|---------|
| `city` | `София` |
| `country` | `България` (default) |
| `date` | `2026-09-15` |
| `time` | `18:00` |
| `registration_link` | registration URL |
| `dry_run` | preview only |

Locally:

```bash
python -m media_publisher --check-event-meta
python -m media_publisher --publish-event \
  --event-type surya_kriya \
  --city "София" \
  --date 2026-09-15 \
  --time 18:00 \
  --registration-link "https://example.com/register" \
  --dry-run
```

## Cleanup

Past events (start time ≤ now, Europe/Sofia) are removed by the **Prune past events** workflow daily (~midnight Sofia) and also whenever a new event is published.
