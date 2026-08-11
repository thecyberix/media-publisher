from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from media_publisher.events.templates import RenderedEvent

EVENTS_DIR_NAME = "events"
EVENTS_DATA_RELATIVE = Path("data") / "events.json"
EVENTS_INDEX_NAME = "index.html"
EVENTS_TIMEZONE = ZoneInfo("Europe/Sofia")
# Metricool SmartLink (https://t-sml.mtrbio.com/public/smartlink/sadhguru-bulgarian)
SMARTLINK_BACKGROUND = "#F9F4F3"
SMARTLINK_TEXT = "#2B0B0B"
SMARTLINK_MUTED = "#6B4A4A"
SMARTLINK_ACCENT = "#5E583A"  # "Събития" button
SMARTLINK_LINK = "#4F6F8B"  # "Водени Медитации" button tone
EMPTY_STATE_TEXT = "Очаквайте скоро!"


@dataclass(frozen=True)
class StoredEvent:
    id: str
    event_type: str
    title: str
    city: str
    country: str
    datetime_iso: str
    datetime_display: str
    registration_link: str
    learn_more_url: str
    html_body: str
    full_text: str
    created_at: str
    facebook_post_id: str | None = None
    facebook_permalink: str | None = None


def event_dedupe_key(rendered: RenderedEvent) -> str:
    raw = "|".join(
        [
            rendered.event_type,
            rendered.city.casefold(),
            rendered.country.casefold(),
            rendered.datetime_iso,
            rendered.registration_link.strip(),
        ]
    )
    return sha1(raw.encode("utf-8")).hexdigest()[:16]


def default_events_root(project_root: Path) -> Path:
    return project_root / EVENTS_DIR_NAME


def events_json_path(events_root: Path) -> Path:
    return events_root / EVENTS_DATA_RELATIVE


def events_index_path(events_root: Path) -> Path:
    return events_root / EVENTS_INDEX_NAME


def load_events(events_root: Path) -> list[dict[str, Any]]:
    path = events_json_path(events_root)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        items = payload.get("events", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError(f"Invalid events JSON shape in {path}")
    if not isinstance(items, list):
        raise ValueError(f"Invalid events list in {path}")
    return [item for item in items if isinstance(item, dict)]


def find_duplicate(events: list[dict[str, Any]], event_id: str) -> dict[str, Any] | None:
    for item in events:
        if str(item.get("id") or "") == event_id:
            return item
    return None


def stored_event_from_dict(item: dict[str, Any]) -> StoredEvent:
    return StoredEvent(
        id=str(item.get("id") or ""),
        event_type=str(item.get("event_type") or ""),
        title=str(item.get("title") or ""),
        city=str(item.get("city") or ""),
        country=str(item.get("country") or ""),
        datetime_iso=str(item.get("datetime_iso") or ""),
        datetime_display=str(item.get("datetime_display") or ""),
        registration_link=str(item.get("registration_link") or ""),
        learn_more_url=str(item.get("learn_more_url") or ""),
        html_body=str(item.get("html_body") or ""),
        full_text=str(item.get("full_text") or ""),
        created_at=str(item.get("created_at") or ""),
        facebook_post_id=(
            str(item["facebook_post_id"]) if item.get("facebook_post_id") else None
        ),
        facebook_permalink=(
            str(item["facebook_permalink"]) if item.get("facebook_permalink") else None
        ),
    )


def parse_stored_event_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=EVENTS_TIMEZONE)
    return parsed


def is_past_event(item: dict[str, Any], *, now: datetime) -> bool:
    event_at = parse_stored_event_datetime(str(item.get("datetime_iso") or ""))
    if event_at is None:
        return False
    return event_at <= now.astimezone(event_at.tzinfo)


def prune_past_events(
    events_root: Path,
    *,
    now: datetime | None = None,
    write: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Remove events whose start datetime is in the past (Europe/Sofia).

    Returns ``(kept, removed)``.
    """
    current = now or datetime.now(EVENTS_TIMEZONE)
    existing = load_events(events_root) if events_root.is_dir() else []
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in existing:
        if is_past_event(item, now=current):
            removed.append(item)
        else:
            kept.append(item)

    if write and (removed or events_root.is_dir()):
        events_root.mkdir(parents=True, exist_ok=True)
        (events_root / "data").mkdir(parents=True, exist_ok=True)
        _write_events_json(events_root, kept)
        rebuild_index(events_root, kept)
    return kept, removed


def append_event(
    events_root: Path,
    rendered: RenderedEvent,
    *,
    facebook_post_id: str | None = None,
    facebook_permalink: str | None = None,
    created_at: datetime | None = None,
    now: datetime | None = None,
) -> tuple[StoredEvent, bool]:
    """Append an event and rebuild index.html.

    Past events are pruned first. Returns ``(stored_event, created)`` where
    ``created`` is False on duplicate.
    """
    events_root.mkdir(parents=True, exist_ok=True)
    (events_root / "data").mkdir(parents=True, exist_ok=True)

    existing, _removed = prune_past_events(events_root, now=now, write=True)
    event_id = event_dedupe_key(rendered)
    duplicate = find_duplicate(existing, event_id)
    if duplicate is not None:
        stored = stored_event_from_dict(duplicate)
        rebuild_index(events_root, existing)
        return stored, False

    stamp = created_at or datetime.now(timezone.utc)
    stored = StoredEvent(
        id=event_id,
        event_type=rendered.event_type,
        title=rendered.title,
        city=rendered.city,
        country=rendered.country,
        datetime_iso=rendered.datetime_iso,
        datetime_display=rendered.datetime_display,
        registration_link=rendered.registration_link,
        learn_more_url=rendered.learn_more_url,
        html_body=rendered.html_body,
        full_text=rendered.full_text,
        created_at=stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
        facebook_post_id=facebook_post_id,
        facebook_permalink=facebook_permalink,
    )
    existing.append(asdict(stored))
    existing.sort(key=lambda item: str(item.get("datetime_iso") or ""), reverse=True)
    _write_events_json(events_root, existing)
    rebuild_index(events_root, existing)
    return stored, True


def rebuild_index(events_root: Path, events: list[dict[str, Any]] | None = None) -> Path:
    items = events if events is not None else load_events(events_root)
    index_path = events_index_path(events_root)
    sections: list[str] = []
    for item in items:
        body = str(item.get("html_body") or "").strip()
        if not body:
            continue
        sections.append(f'<article class="event" id="event-{_html_escape(str(item.get("id") or ""))}">')
        sections.append(body)
        permalink = item.get("facebook_permalink")
        if isinstance(permalink, str) and permalink.strip():
            sections.append(
                f'<p class="fb"><a href="{_html_escape(permalink.strip())}">Facebook пост</a></p>'
            )
        sections.append("</article>")

    if sections:
        body_class = ""
        main_html = "\n".join(sections)
        chrome = _LIST_CHROME.format(events=main_html)
    else:
        body_class = ' class="is-empty"'
        chrome = (
            f'<p class="coming-soon">{_html_escape(EMPTY_STATE_TEXT)}</p>'
        )

    html = _INDEX_TEMPLATE.format(
        background=SMARTLINK_BACKGROUND,
        text=SMARTLINK_TEXT,
        muted=SMARTLINK_MUTED,
        accent=SMARTLINK_ACCENT,
        link=SMARTLINK_LINK,
        body_class=body_class,
        chrome=chrome,
    )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(html, encoding="utf-8", newline="\n")
    return index_path


def _write_events_json(events_root: Path, events: list[dict[str, Any]]) -> None:
    path = events_json_path(events_root)
    payload = {"events": events}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_LIST_CHROME = """\
  <header>
    <h1>Събития — Садгуру България</h1>
    <p>Обявени програми на Иша в България.</p>
  </header>
  <main>
{events}
  </main>
  <footer>
    Доброволци от Иша · Садгуру България
  </footer>"""


_INDEX_TEMPLATE = """\
<!DOCTYPE html>
<html lang="bg">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Събития — Садгуру България</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Merriweather:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: {background};
      --ink: {text};
      --muted: {muted};
      --accent: {accent};
      --link: {link};
      --rule: color-mix(in srgb, var(--ink) 14%, transparent);
      --button-fg: #f1f3f5;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      height: 100%;
    }}
    body {{
      margin: 0;
      font-family: Merriweather, Georgia, "Times New Roman", serif;
      color: var(--ink);
      background: var(--bg);
      line-height: 1.55;
      text-align: center;
    }}
    body.is-empty {{
      min-height: 100%;
      display: grid;
      place-items: center;
      padding: 1.5rem;
    }}
    .coming-soon {{
      margin: 0;
      text-align: center;
      font-size: clamp(2.2rem, 9vw, 4.5rem);
      font-weight: 700;
      letter-spacing: -0.02em;
      line-height: 1.15;
      color: var(--ink);
    }}
    header {{
      padding: 3rem 1.25rem 1.25rem;
      max-width: 28rem;
      margin: 0 auto;
    }}
    header h1 {{
      margin: 0 0 0.5rem;
      font-size: clamp(1.6rem, 4vw, 2rem);
      font-weight: 700;
      line-height: 1.25;
    }}
    header p {{
      margin: 0;
      color: var(--muted);
      font-size: 1.05rem;
    }}
    main {{
      max-width: 28rem;
      margin: 0 auto;
      padding: 0 1.25rem 3rem;
      display: grid;
      gap: 2rem;
      text-align: left;
    }}
    .event {{
      padding: 1.5rem 0 0;
      border-top: 1px solid var(--rule);
    }}
    .event h2 {{
      margin: 0 0 0.75rem;
      font-size: 1.2rem;
      line-height: 1.35;
      text-align: center;
    }}
    .when {{
      color: var(--muted);
      font-weight: 700;
      text-align: center;
    }}
    .quote {{
      font-style: italic;
    }}
    .cta {{
      text-align: center;
      margin: 1.25rem 0;
    }}
    .cta a {{
      display: inline-block;
      width: 100%;
      max-width: 22rem;
      padding: 0.75rem 1rem;
      background: var(--accent);
      color: var(--button-fg);
      font-weight: 700;
      font-size: 1.05rem;
      text-decoration: none;
      border-radius: 30px;
    }}
    .cta a:hover {{
      filter: brightness(1.05);
    }}
    a {{ color: var(--link); }}
    ul {{
      padding-left: 1.2rem;
      text-align: left;
    }}
    .fb {{
      font-size: 0.95rem;
      text-align: center;
    }}
    footer {{
      max-width: 28rem;
      margin: 0 auto;
      padding: 0 1.25rem 3rem;
      color: var(--muted);
      font-size: 0.95rem;
    }}
  </style>
</head>
<body{body_class}>
{chrome}
</body>
</html>
"""
