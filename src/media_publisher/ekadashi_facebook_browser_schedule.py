"""Local Playwright scheduling for Ekadashi Facebook + Instagram photo posts.

Exports pages from the Canva ``Ekadashi {year}`` design, pairs each with the
hard-coded 2025 caption for that chronological slot, and schedules via Business
Suite with Instagram left selected (cross-post).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from media_publisher.ekadashi import (
    CANVA_EKADASHI_FOLDER_ID,
    EkadashiCalendarError,
    EkadashiEntry,
    ekadashi_entries_for_month,
    parse_ekadashi_design_year,
)
from media_publisher.publishers.facebook_web import (
    FacebookWebError,
    publish_facebook_photo_via_browser,
    resolve_facebook_browser_channel,
    resolve_facebook_browser_profile_dir,
    resolve_facebook_browser_state_path,
)
from media_publisher.sources.canva import CanvaClient, CanvaDesignSummary, CanvaError
from media_publisher.sources.google_drive import local_file_md5
from media_publisher.timezones import get_timezone

HISTORY_RELATIVE_PATH = Path("downloads/ekadashi/facebook-browser-schedule-history.json")
CACHE_RELATIVE_DIR = Path("downloads/ekadashi/browser-schedule-cache")

# Posts go live the calendar day before Ekadashi at 12:30 Europe/Sofia (FB + IG).
DEFAULT_SCHEDULE_DAYS_BEFORE = 1
DEFAULT_SCHEDULE_HOUR = 12
DEFAULT_SCHEDULE_MINUTE = 30


class EkadashiFacebookScheduleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedEkadashiDesign:
    design_id: str
    title: str
    design_year: int
    requested_year: int

    @property
    def is_fallback(self) -> bool:
        return self.design_year != self.requested_year


@dataclass(frozen=True)
class PreparedEkadashiScheduleItem:
    entry: EkadashiEntry
    caption: str
    publish_at: datetime
    image_path: Path
    content_fingerprint: str
    canva_design_id: str


@dataclass(frozen=True)
class ScheduleSkip:
    stem: str
    reason: str


def default_history_path(project_root: Path) -> Path:
    return project_root / HISTORY_RELATIVE_PATH


def default_cache_dir(project_root: Path, *, year: int, month: int) -> Path:
    return project_root / CACHE_RELATIVE_DIR / f"{year:04d}-{month:02d}"


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
    stem: str,
    permalink: str,
    publish_at: datetime,
    content_fingerprint: str,
    canva_design_id: str = "",
    scheduled_at: datetime | None = None,
) -> None:
    when = scheduled_at or datetime.now(tz=publish_at.tzinfo)
    entry: dict[str, Any] = {
        "permalink": permalink,
        "scheduled_at": when.isoformat(),
        "publish_at": publish_at.isoformat(),
        "content_fingerprint": content_fingerprint,
    }
    if canva_design_id:
        entry["canva_design_id"] = canva_design_id
    history[stem] = entry


def list_ekadashi_designs_by_year(
    client: CanvaClient,
    *,
    folder_id: str = CANVA_EKADASHI_FOLDER_ID,
) -> dict[int, CanvaDesignSummary]:
    """Map design year → Canva design for titles matching ``Ekadashi YYYY``."""
    by_year: dict[int, CanvaDesignSummary] = {}
    continuation: str | None = None
    while True:
        try:
            designs, continuation = client.list_folder_designs(
                folder_id,
                continuation=continuation,
                limit=100,
            )
        except CanvaError as exc:
            raise EkadashiFacebookScheduleError(
                f"{exc}. Open https://www.canva.com/folder/{folder_id}"
            ) from exc
        for design in designs:
            design_year = parse_ekadashi_design_year(design.title)
            if design_year is None:
                continue
            previous = by_year.get(design_year)
            if previous is None:
                by_year[design_year] = design
        if not continuation:
            break
    return by_year


def find_ekadashi_design(
    client: CanvaClient,
    *,
    year: int,
    folder_id: str = CANVA_EKADASHI_FOLDER_ID,
) -> ResolvedEkadashiDesign:
    """Resolve ``Ekadashi {year}``, or the latest available year design as fallback."""
    by_year = list_ekadashi_designs_by_year(client, folder_id=folder_id)
    if not by_year:
        raise EkadashiFacebookScheduleError(
            f"No Canva designs titled 'Ekadashi YYYY' in folder {folder_id}. "
            f"Open https://www.canva.com/folder/{folder_id}"
        )

    design = by_year.get(year)
    design_year = year
    if design is None:
        design_year = max(by_year)
        design = by_year[design_year]

    return ResolvedEkadashiDesign(
        design_id=design.id,
        title=design.title,
        design_year=design_year,
        requested_year=year,
    )


def export_ekadashi_page(
    client: CanvaClient,
    *,
    design_id: str,
    page_number: int,
    destination: Path,
    export_format: str = "jpg",
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    try:
        return client.download_design_image(
            design_id,
            destination,
            export_format=export_format,
            pages=[page_number],
        )
    except CanvaError as exc:
        raise EkadashiFacebookScheduleError(
            f"Failed to export Canva page {page_number} of {design_id}: {exc}"
        ) from exc


def ekadashi_schedule_at(
    event_date: date,
    *,
    publish_timezone: str,
    days_before: int = DEFAULT_SCHEDULE_DAYS_BEFORE,
    hour: int = DEFAULT_SCHEDULE_HOUR,
    minute: int = DEFAULT_SCHEDULE_MINUTE,
) -> datetime:
    """Local datetime when the FB+IG post should be published (day before @ 12:30)."""
    tz = get_timezone(publish_timezone)
    publish_day = event_date - timedelta(days=days_before)
    return datetime(
        publish_day.year,
        publish_day.month,
        publish_day.day,
        hour,
        minute,
        0,
        tzinfo=tz,
    )


def discover_ekadashi_for_month(
    *,
    canva_client: CanvaClient,
    project_root: Path,
    year: int,
    month: int,
    publish_timezone: str,
    publish_hour: int = DEFAULT_SCHEDULE_HOUR,
    publish_minute: int = DEFAULT_SCHEDULE_MINUTE,
    days_before: int = DEFAULT_SCHEDULE_DAYS_BEFORE,
    day: int | None = None,
    folder_id: str = CANVA_EKADASHI_FOLDER_ID,
    cache_dir: Path | None = None,
    ics_path: Path | None = None,
    calendar_id: str | None = None,
) -> tuple[list[PreparedEkadashiScheduleItem], list[str]]:
    """Export Canva pages and build schedule items for one calendar month.

    Dates come from the public Isha Google Calendar ICS feed (all-day events).
    ``publish_at`` is ``days_before`` the Ekadashi date at ``publish_hour``:``publish_minute``
    in ``publish_timezone`` (typically Europe/Sofia), applied to Facebook + Instagram.
    """
    warnings: list[str] = []
    try:
        entries = ekadashi_entries_for_month(
            year,
            month,
            ics_path=ics_path,
            **({"calendar_id": calendar_id} if calendar_id else {}),
        )
    except (ValueError, EkadashiCalendarError) as exc:
        raise EkadashiFacebookScheduleError(str(exc)) from exc

    if day is not None:
        entries = tuple(e for e in entries if e.day == day)
        if not entries:
            raise EkadashiFacebookScheduleError(
                f"No Ekadashi on {year:04d}-{month:02d}-{day:02d}"
            )

    if not entries:
        return [], [f"No Ekadashi dates in {year:04d}-{month:02d}"]

    warnings.append(
        f"Dates from Isha calendar ICS"
        + (f" ({ics_path})" if ics_path is not None else "")
        + f"; schedule {days_before} day(s) before at "
        f"{publish_hour:02d}:{publish_minute:02d} {publish_timezone} (FB+IG)"
    )

    resolved = find_ekadashi_design(
        canva_client, year=year, folder_id=folder_id
    )
    if resolved.is_fallback:
        warnings.append(
            f"Canva design for {year} not found; "
            f"falling back to {resolved.title!r} ({resolved.design_id})"
        )
    else:
        warnings.append(f"Canva design: {resolved.title} ({resolved.design_id})")

    dest_root = cache_dir or default_cache_dir(
        project_root, year=year, month=month
    )
    items: list[PreparedEkadashiScheduleItem] = []
    for entry in entries:
        image_path = export_ekadashi_page(
            canva_client,
            design_id=resolved.design_id,
            page_number=entry.page_number,
            destination=dest_root
            / f"{entry.stem}_page{entry.page_number}.jpg",
        )
        publish_at = ekadashi_schedule_at(
            entry.event_date,
            publish_timezone=publish_timezone,
            days_before=days_before,
            hour=publish_hour,
            minute=publish_minute,
        )
        caption = entry.caption.strip()
        items.append(
            PreparedEkadashiScheduleItem(
                entry=entry,
                caption=caption,
                publish_at=publish_at,
                image_path=image_path,
                content_fingerprint=content_fingerprint(
                    image_path=image_path, caption=caption
                ),
                canva_design_id=resolved.design_id,
            )
        )
    return items, warnings


def select_pending_schedule_items(
    items: list[PreparedEkadashiScheduleItem],
    history: dict[str, dict[str, Any]],
    *,
    now: datetime,
    force: bool,
    min_lead_minutes: int = 15,
) -> tuple[list[PreparedEkadashiScheduleItem], list[ScheduleSkip]]:
    from datetime import timedelta

    pending: list[PreparedEkadashiScheduleItem] = []
    skips: list[ScheduleSkip] = []
    min_lead = timedelta(minutes=min_lead_minutes)
    for item in items:
        stem = item.entry.stem
        if not force:
            entry = history.get(stem)
            if isinstance(entry, dict):
                permalink = entry.get("permalink")
                previous = entry.get("content_fingerprint")
                if (
                    isinstance(permalink, str)
                    and permalink.strip()
                    and isinstance(previous, str)
                    and previous == item.content_fingerprint
                ):
                    skips.append(ScheduleSkip(stem=stem, reason="already in history"))
                    continue
        if item.publish_at <= now + min_lead:
            skips.append(
                ScheduleSkip(
                    stem=stem,
                    reason=(
                        f"publish_at {item.publish_at.isoformat()} is too soon / past"
                    ),
                )
            )
            continue
        pending.append(item)
    return pending, skips


def schedule_ekadashi_via_browser(
    items: list[PreparedEkadashiScheduleItem],
    *,
    page_username: str,
    project_root: Path,
    history: dict[str, dict[str, Any]],
    history_path: Path,
    display_timezone: str,
    dry_run: bool = False,
    print_line: Callable[[str], None] | None = None,
) -> tuple[int, int, list[str]]:
    """Schedule posts to Facebook + Instagram. Returns (ok, failed, errors)."""
    log = print_line or print
    ok = 0
    failed = 0
    errors: list[str] = []
    for item in items:
        stem = item.entry.stem
        log(
            f"{stem}: {item.entry.name} (Ekadashi {stem}) → "
            f"FB+IG {item.publish_at.isoformat()} "
            f"(page {item.entry.page_number}, {item.image_path.name})"
        )
        if dry_run:
            preview = (
                item.caption if len(item.caption) <= 100 else item.caption[:97] + "..."
            )
            log(f"  dry-run caption: {preview}")
            ok += 1
            continue
        screenshot = (
            project_root / "downloads" / f"facebook-browser-ekadashi-failure-{stem}.png"
        )
        try:
            _post_id, permalink = publish_facebook_photo_via_browser(
                page_username=page_username,
                image_path=item.image_path,
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
                include_instagram=True,
                failure_screenshot=screenshot,
            )
        except FacebookWebError as exc:
            failed += 1
            message = f"{stem}: {exc}"
            errors.append(message)
            log(f"  ERROR: {exc}")
            continue
        mark_scheduled_in_history(
            history,
            stem=stem,
            permalink=permalink,
            publish_at=item.publish_at,
            content_fingerprint=item.content_fingerprint,
            canva_design_id=item.canva_design_id,
        )
        save_schedule_history(history_path, history)
        log(f"  scheduled FB+IG ({permalink})")
        ok += 1
    return ok, failed, errors
