"""Schedule prepared Drive quote JPEGs to Facebook via Playwright.

Used locally: reads the generated-quotes month folder under Drive ``Quotes``,
schedules each new day with Business Suite, and records history so re-runs skip
already uploaded days.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from media_publisher.post_templates import build_quote_post_caption
from media_publisher.publishers.facebook_web import (
    FacebookWebError,
    publish_facebook_photo_via_browser,
    resolve_facebook_browser_channel,
    resolve_facebook_browser_profile_dir,
    resolve_facebook_browser_state_path,
)
from media_publisher.quotes_drive_sync import GENERATED_MONTH_FOLDER_PATTERN
from media_publisher.quotes_text_sync import resolve_bulgarian_spreadsheet_id
from media_publisher.sources.drive_layout import resolve_quotes_folder_id
from media_publisher.sources.google_drive import (
    DriveFile,
    GoogleDriveClient,
    GoogleDriveError,
    format_month_folder_name,
    local_file_md5,
)
from media_publisher.sources.google_sheets import GoogleSheetsClient, GoogleSheetsError
from media_publisher.sources.quotes_config import QuotesSourcesConfig
from media_publisher.sources.quotes_sheet import QuotesSheetError, load_monthly_quote_texts
from media_publisher.timezones import get_timezone

HISTORY_RELATIVE_PATH = Path("downloads/quotes/facebook-browser-schedule-history.json")
CACHE_RELATIVE_DIR = Path("downloads/quotes/browser-schedule-cache")
QUOTE_DRIVE_NAME_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\.jpe?g$",
    re.IGNORECASE,
)


class QuotesFacebookScheduleError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedQuoteScheduleItem:
    year: int
    month: int
    day: int
    stem: str
    drive_name: str
    drive_file_id: str
    caption: str
    publish_at: datetime
    image_path: Path
    content_fingerprint: str


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
    drive_file_id: str = "",
    scheduled_at: datetime | None = None,
) -> None:
    when = scheduled_at or datetime.now(tz=publish_at.tzinfo)
    entry: dict[str, Any] = {
        "permalink": permalink,
        "publish_at": publish_at.isoformat(),
        "scheduled_at": when.isoformat(),
        "content_fingerprint": content_fingerprint,
    }
    if drive_file_id:
        entry["drive_file_id"] = drive_file_id
    history[stem] = entry


def parse_quote_drive_name(
    name: str, *, year: int, month: int
) -> tuple[int, str] | None:
    match = QUOTE_DRIVE_NAME_RE.match(name.strip())
    if not match:
        return None
    file_year = int(match.group("year"))
    file_month = int(match.group("month"))
    day = int(match.group("day"))
    if file_year != year or file_month != month:
        return None
    days_in_month = calendar.monthrange(year, month)[1]
    if day < 1 or day > days_in_month:
        return None
    stem = f"{year:04d}-{month:02d}-{day:02d}"
    return day, stem


def list_prepared_quote_files(
    drive: GoogleDriveClient,
    *,
    year: int,
    month: int,
    quotes_root_id: str | None = None,
) -> list[DriveFile]:
    root_id = quotes_root_id or resolve_quotes_folder_id(drive)
    folder_name = format_month_folder_name(
        GENERATED_MONTH_FOLDER_PATTERN,
        year=year,
        month=month,
    )
    folder = drive.find_child_folder(root_id, folder_name)
    if folder is None:
        raise QuotesFacebookScheduleError(
            f"Drive month folder {folder_name!r} not found under Quotes. "
            "Run scripts/render_monthly_quotes.py --sync-drive first."
        )
    files: list[DriveFile] = []
    for item in drive.list_children(folder.id):
        if item.mime_type == "application/vnd.google-apps.folder":
            continue
        parsed = parse_quote_drive_name(item.name, year=year, month=month)
        if parsed is None:
            continue
        files.append(item)
    files.sort(key=lambda item: item.name)
    return files


def discover_prepared_quotes_for_month(
    *,
    config: QuotesSourcesConfig,
    sheets_client: GoogleSheetsClient,
    drive_client: GoogleDriveClient,
    project_root: Path,
    year: int,
    month: int,
    publish_timezone: str,
    publish_hour: int,
    day: int | None = None,
    cache_dir: Path | None = None,
) -> tuple[list[PreparedQuoteScheduleItem], list[str]]:
    """Download Drive prepared JPEGs and pair them with Ready sheet captions."""
    warnings: list[str] = []
    try:
        spreadsheet_id = resolve_bulgarian_spreadsheet_id(
            drive=drive_client, config=config, year=year
        )
        quotes = load_monthly_quote_texts(
            sheets_client,
            config,
            year=year,
            month=month,
            require_ready=True,
            spreadsheet_id=spreadsheet_id,
        )
    except (QuotesSheetError, GoogleSheetsError) as exc:
        raise QuotesFacebookScheduleError(
            f"Could not load Ready captions for {year:04d}-{month:02d}: {exc}"
        ) from exc
    except Exception as exc:
        raise QuotesFacebookScheduleError(
            f"Could not load Ready captions for {year:04d}-{month:02d}: {exc}"
        ) from exc

    captions = {quote.day: quote.text_bg for quote in quotes if quote.text_bg.strip()}
    try:
        drive_files = list_prepared_quote_files(
            drive_client, year=year, month=month
        )
    except GoogleDriveError as exc:
        raise QuotesFacebookScheduleError(str(exc)) from exc

    target_cache = cache_dir or default_cache_dir(
        project_root, year=year, month=month
    )
    target_cache.mkdir(parents=True, exist_ok=True)

    items: list[PreparedQuoteScheduleItem] = []
    for drive_file in drive_files:
        parsed = parse_quote_drive_name(drive_file.name, year=year, month=month)
        if parsed is None:
            continue
        file_day, stem = parsed
        if day is not None and file_day != day:
            continue
        caption_text = captions.get(file_day, "").strip()
        if not caption_text:
            warnings.append(
                f"{stem}: Drive image present but Ready caption missing — skipped"
            )
            continue
        local_path = target_cache / drive_file.name
        try:
            drive_client.download_file(drive_file.id, local_path)
        except GoogleDriveError as exc:
            warnings.append(f"{stem}: download failed ({exc})")
            continue
        caption = build_quote_post_caption(caption_text)
        publish_at = datetime(
            year,
            month,
            file_day,
            publish_hour,
            0,
            tzinfo=get_timezone(publish_timezone),
        )
        items.append(
            PreparedQuoteScheduleItem(
                year=year,
                month=month,
                day=file_day,
                stem=stem,
                drive_name=drive_file.name,
                drive_file_id=drive_file.id,
                caption=caption,
                publish_at=publish_at,
                image_path=local_path,
                content_fingerprint=content_fingerprint(
                    image_path=local_path, caption=caption
                ),
            )
        )
    return items, warnings


def select_pending_schedule_items(
    items: list[PreparedQuoteScheduleItem],
    history: dict[str, dict[str, Any]],
    *,
    now: datetime,
    force: bool = False,
    min_lead: timedelta = timedelta(minutes=15),
) -> tuple[list[PreparedQuoteScheduleItem], list[ScheduleSkip]]:
    """Return items still needing a Facebook schedule, plus skip reasons."""
    pending: list[PreparedQuoteScheduleItem] = []
    skips: list[ScheduleSkip] = []
    for item in items:
        entry = history.get(item.stem)
        has_permalink = (
            isinstance(entry, dict)
            and isinstance(entry.get("permalink"), str)
            and bool(str(entry.get("permalink")).strip())
        )
        same_content = (
            has_permalink
            and entry is not None
            and entry.get("content_fingerprint") == item.content_fingerprint
        )
        if not force and same_content:
            skips.append(ScheduleSkip(item.stem, "already in history"))
            continue
        if item.publish_at <= now + min_lead:
            skips.append(
                ScheduleSkip(
                    item.stem,
                    f"publish_at {item.publish_at.isoformat()} is too soon / past",
                )
            )
            continue
        pending.append(item)
    return pending, skips


def schedule_prepared_quotes_via_browser(
    items: list[PreparedQuoteScheduleItem],
    *,
    page_username: str,
    project_root: Path,
    history: dict[str, dict[str, Any]],
    history_path: Path,
    display_timezone: str,
    dry_run: bool = False,
    print_line: Callable[[str], None] | None = None,
) -> tuple[int, int, list[str]]:
    """Schedule items; updates history after each success. Returns (ok, failed, errors)."""
    log = print_line or print
    ok = 0
    failed = 0
    errors: list[str] = []
    for item in items:
        log(
            f"{item.stem}: schedule {item.publish_at.isoformat()} "
            f"({item.image_path.name})"
        )
        if dry_run:
            preview = item.caption if len(item.caption) <= 80 else item.caption[:77] + "..."
            log(f"  dry-run caption: {preview}")
            ok += 1
            continue
        screenshot = (
            project_root
            / "downloads"
            / f"facebook-browser-schedule-failure-{item.stem}.png"
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
                # Same options as scripts/poc_facebook_browser_photo.py.
                headless=False,
                proxy=None,
                failure_screenshot=screenshot,
            )
        except FacebookWebError as exc:
            failed += 1
            message = f"{item.stem}: {exc}"
            errors.append(message)
            log(f"  failed — {exc}")
            continue
        mark_scheduled_in_history(
            history,
            stem=item.stem,
            permalink=permalink,
            publish_at=item.publish_at,
            content_fingerprint=item.content_fingerprint,
            drive_file_id=item.drive_file_id,
        )
        save_schedule_history(history_path, history)
        ok += 1
        log(f"  scheduled ({permalink})")
    return ok, failed, errors
