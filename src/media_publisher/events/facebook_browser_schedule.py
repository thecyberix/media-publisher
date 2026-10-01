"""Local Playwright scheduling for Facebook event photo posts.

Mirrors quotes_facebook_browser_schedule: render caption, pick Drive image,
schedule via Business Suite, keep a history so re-runs skip duplicates.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from media_publisher.events.drive_copy import (
    EventTemplateError,
    load_program_from_drive,
)
from media_publisher.events.facebook_event import (
    EventImageError,
    SelectedEventImage,
    resolve_facebook_image_from_drive,
)
from media_publisher.events.format import parse_event_date, parse_event_time
from media_publisher.events.page import (
    append_event,
    default_events_root,
    event_dedupe_key,
    find_duplicate,
    load_events,
)
from media_publisher.events.templates import (
    EVENT_TYPE_BHUTA_SHUDDHI,
    EVENT_TYPE_SURYA_KRIYA,
    EVENT_TYPE_YOGASANA,
    RenderedEvent,
    render_event,
)
from media_publisher.publishers.facebook_web import (
    FacebookWebError,
    publish_facebook_photo_via_browser,
    resolve_facebook_browser_channel,
    resolve_facebook_browser_profile_dir,
    resolve_facebook_browser_state_path,
)
from media_publisher.sources.google_drive import GoogleDriveClient, local_file_md5

HISTORY_RELATIVE_PATH = Path("downloads/events/facebook-browser-schedule-history.json")
EVENT_TYPE_CHOICES = (
    EVENT_TYPE_SURYA_KRIYA,
    EVENT_TYPE_BHUTA_SHUDDHI,
    EVENT_TYPE_YOGASANA,
    "yogasanas",
)


class EventFacebookScheduleError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedEventScheduleItem:
    dedupe_key: str
    rendered: RenderedEvent
    image: SelectedEventImage
    caption: str
    content_fingerprint: str
    publish_at: datetime | None


def default_history_path(project_root: Path) -> Path:
    return project_root / HISTORY_RELATIVE_PATH


def content_fingerprint(*, image_path: Path, caption: str) -> str:
    payload = f"{local_file_md5(image_path)}|{caption.strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_schedule_history(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(key, str) and isinstance(value, dict):
            result[key] = dict(value)
    return result


def save_schedule_history(path: Path, history: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def mark_scheduled_in_history(
    history: dict[str, dict[str, Any]],
    *,
    dedupe_key: str,
    permalink: str,
    publish_at: datetime | None,
    content_fingerprint: str,
    drive_file_id: str = "",
    scheduled_at: datetime | None = None,
) -> None:
    when = scheduled_at or datetime.now()
    entry: dict[str, Any] = {
        "permalink": permalink,
        "scheduled_at": when.isoformat(),
        "content_fingerprint": content_fingerprint,
    }
    if publish_at is not None:
        entry["publish_at"] = publish_at.isoformat()
    if drive_file_id:
        entry["drive_file_id"] = drive_file_id
    history[dedupe_key] = entry


def history_blocks_item(
    history: dict[str, dict[str, Any]],
    *,
    dedupe_key: str,
    content_fingerprint: str,
    force: bool,
) -> bool:
    if force:
        return False
    entry = history.get(dedupe_key)
    if not isinstance(entry, dict):
        return False
    permalink = entry.get("permalink")
    if not isinstance(permalink, str) or not permalink.strip():
        return False
    previous = entry.get("content_fingerprint")
    if isinstance(previous, str) and previous and previous != content_fingerprint:
        return False
    return True


def prepare_event_for_facebook_schedule(
    *,
    project_root: Path,
    drive_client: GoogleDriveClient,
    event_type: str,
    city: str,
    country: str,
    date_text: str,
    time_text: str,
    registration_link: str,
    image_id: str | None,
    publish_at: datetime | None,
    language: str = "bg",
    events_root: Path | None = None,
) -> PreparedEventScheduleItem:
    """Render caption, resolve Drive image, build schedule item."""
    try:
        event_date = parse_event_date(date_text)
        event_time = parse_event_time(time_text)
        program = load_program_from_drive(
            drive_client,
            event_type,
            project_root=project_root,
        )
        rendered = render_event(
            event_type=event_type,
            city=city,
            country=country,
            event_date=event_date,
            event_time=event_time,
            registration_link=registration_link,
            program=program,
            language=language,
        )
    except (ValueError, EventTemplateError) as exc:
        raise EventFacebookScheduleError(str(exc)) from exc

    root = events_root or default_events_root(project_root)
    try:
        selected = resolve_facebook_image_from_drive(
            project_root=project_root,
            events_root=root,
            event_type=rendered.event_type,
            drive_client=drive_client,
            image_id=image_id,
            persist_rotation=True,
        )
    except EventImageError as exc:
        raise EventFacebookScheduleError(str(exc)) from exc

    caption = rendered.facebook_post_text.strip()
    if not caption:
        raise EventFacebookScheduleError("Rendered Facebook caption is empty")

    return PreparedEventScheduleItem(
        dedupe_key=event_dedupe_key(rendered),
        rendered=rendered,
        image=selected,
        caption=caption,
        content_fingerprint=content_fingerprint(
            image_path=selected.local_path, caption=caption
        ),
        publish_at=publish_at,
    )


def schedule_event_via_browser(
    item: PreparedEventScheduleItem,
    *,
    page_username: str,
    project_root: Path,
    history: dict[str, dict[str, Any]],
    history_path: Path,
    display_timezone: str,
    dry_run: bool = False,
    update_page: bool = True,
    events_root: Path | None = None,
    print_line: Callable[[str], None] | None = None,
) -> str:
    """Schedule/post one event via Playwright. Returns permalink. Updates history."""
    log = print_line or print
    root = events_root or default_events_root(project_root)
    when = (
        item.publish_at.isoformat()
        if item.publish_at is not None
        else "now (immediate)"
    )
    title_preview = " ".join(item.rendered.title.split())
    log(
        f"{item.dedupe_key}: {title_preview} → {when} "
        f"({item.image.drive_file.name})"
    )
    if dry_run:
        preview = item.caption if len(item.caption) <= 120 else item.caption[:117] + "..."
        log(f"  dry-run caption: {preview}")
        return "dry-run"

    screenshot = (
        project_root
        / "downloads"
        / f"facebook-browser-event-failure-{item.dedupe_key}.png"
    )
    try:
        _post_id, permalink = publish_facebook_photo_via_browser(
            page_username=page_username,
            image_path=item.image.local_path,
            caption=item.caption,
            publish_at=item.publish_at,
            display_timezone=display_timezone,
            project_root=project_root,
            storage_state_path=resolve_facebook_browser_state_path(
                project_root=project_root
            ),
            browser_profile_dir=resolve_facebook_browser_profile_dir(
                project_root=project_root
            ),
            browser_channel=resolve_facebook_browser_channel(),
            headless=False,
            proxy=None,
            failure_screenshot=screenshot,
        )
    except FacebookWebError as exc:
        raise EventFacebookScheduleError(str(exc)) from exc

    mark_scheduled_in_history(
        history,
        dedupe_key=item.dedupe_key,
        permalink=permalink,
        publish_at=item.publish_at,
        content_fingerprint=item.content_fingerprint,
        drive_file_id=item.image.drive_file.id,
    )
    save_schedule_history(history_path, history)
    log(f"  scheduled ({permalink})")

    if update_page:
        existing = load_events(root) if root.is_dir() else []
        duplicate = find_duplicate(existing, item.dedupe_key)
        if duplicate is not None:
            log(f"  page: already listed as {item.dedupe_key} — left unchanged")
        else:
            stored, created = append_event(
                root,
                item.rendered,
                facebook_post_id=permalink,
                facebook_permalink=permalink,
                facebook_image_id=item.image.drive_file.id,
                facebook_image_name=item.image.drive_file.name,
            )
            log(
                f"  page: {'added' if created else 'updated'} {stored.id} "
                f"→ {root / 'index.html'}"
            )
    return permalink
