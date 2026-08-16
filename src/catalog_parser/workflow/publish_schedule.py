from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_SYNC_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_SHORT, TYPE_VIDEO

FIELD_SG_YT_DATE = "SG-YT-Date published"
FIELD_SG_FB_DATE = "SG-FB-Date published"
FIELD_SG_IG_DATE = "SG-IG-Date published"
FIELD_SG_YT_PUBLISHED = "SG-YT-Published video"
FIELD_SG_FB_PUBLISHED = "SG-FB-Published video"
FIELD_SG_IG_PUBLISHED = "SG-IG-Published video"

PLATFORM_DATE_FIELDS = (FIELD_SG_YT_DATE, FIELD_SG_FB_DATE, FIELD_SG_IG_DATE)
PLATFORM_PUBLISHED_FIELDS = (
    FIELD_SG_YT_PUBLISHED,
    FIELD_SG_FB_PUBLISHED,
    FIELD_SG_IG_PUBLISHED,
)

TYPE_QUOTE = "Quote"
STATUS_DONE_PUBLISHED = "Done & Published"


@dataclass(frozen=True)
class ScheduleTomorrowResult:
    success: bool
    message: str
    record_id: str | None = None
    target_date: date | None = None
    applied: bool = False
    missing_prepared_thumbnail_notified: bool = False


def _publish_settings() -> tuple[str, int]:
    from media_publisher.runtime_env import load_publish_timing

    timing = load_publish_timing()
    if not timing.timezone:
        raise RuntimeError("PUBLISH_JSON timezone is required")
    if timing.videos_hour is None:
        raise RuntimeError("PUBLISH_JSON videos_hour is required")
    return timing.timezone, timing.videos_hour


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


def _parse_date_field(value: Any) -> date | None:
    text = _field_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _iso_date(value: date) -> str:
    return value.isoformat()


def desired_type_for_publish_date(target_date: date) -> str:
    """Saturday => long-form Video; other days => short-form Reel/Short."""
    return TYPE_VIDEO if target_date.weekday() == 5 else TYPE_REEL


def has_original_video_thumbnail(fields: dict[str, Any]) -> bool:
    value = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    return isinstance(value, list) and bool(value)


def has_video_name_translated(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)) is not None


def is_quote_record(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_TYPE)) == TYPE_QUOTE


def is_sync_done_status(value: Any) -> bool:
    text = _field_text(value)
    return bool(text and STATUS_SYNC_DONE in text)


def is_done_published_status(value: Any) -> bool:
    text = _field_text(value)
    return bool(text and STATUS_DONE_PUBLISHED in text)


def is_video_type(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_TYPE)) == TYPE_VIDEO


def is_reel_type(fields: dict[str, Any]) -> bool:
    return _field_text(fields.get(FIELD_TYPE)) in {TYPE_REEL, TYPE_SHORT}


def video_format_from_type(type_value: Any) -> str:
    return "post" if _field_text(type_value) == TYPE_VIDEO else "short_form"


def instagram_schedule_excluded(fields: dict[str, Any]) -> bool:
    """True when Instagram should not receive a schedule date (Type=Video)."""
    return is_video_type(fields)


def append_template_links(
    lines: list[str],
    *,
    canva_design: str | None,
    tn_template: str | None,
) -> None:
    if canva_design:
        lines.append(f"   Canva design: {canva_design}")
    if tn_template:
        lines.append(f"   Drive TN template: {tn_template}")
    if not canva_design and not tn_template:
        lines.append("   Canva design / Drive TN template: (not set)")


def format_missing_prepared_thumbnail_email(
    *,
    title: str,
    translated: str | None,
    canva_design: str | None,
    target_date: date,
    tn_template: str | None = None,
) -> tuple[str, str]:
    subject = (
        f"Scheduled for {target_date.isoformat()} — missing prepared thumbnail"
    )
    lines = [
        "A video was scheduled for publishing with an Original Video Thumbnail,",
        "but no prepared thumbnail was found in the Canva catalog or Drive",
        "Thumbnails folder.",
        "",
        f"Publish date: {target_date.isoformat()}",
        "",
        f"1. {title}",
    ]
    if translated:
        lines.append(f"   Translated: {translated}")
    append_template_links(lines, canva_design=canva_design, tn_template=tn_template)
    body = "\n".join(lines).rstrip() + "\n"
    return subject, body


def send_missing_prepared_thumbnail_email(
    *,
    title: str,
    translated: str | None,
    canva_design: str | None,
    target_date: date,
    tn_template: str | None = None,
) -> bool:
    scripts_dir = Path(__file__).resolve().parents[3] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from send_notification_email import send_email

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    notify_email = os.getenv("NOTIFY_EMAIL", "").strip()
    if not smtp_user or not smtp_password or not notify_email:
        return False

    subject, body = format_missing_prepared_thumbnail_email(
        title=title,
        translated=translated,
        canva_design=canva_design,
        target_date=target_date,
        tn_template=tn_template,
    )
    send_email(
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_address=notify_email,
        subject=subject,
        body=body,
    )
    return True


def _optional_canva_client(project_root: Path) -> Any | None:
    client_id = os.getenv("CANVA_CLIENT_ID", "").strip()
    client_secret = os.getenv("CANVA_CLIENT_SECRET", "").strip()
    token_setting = os.getenv("CANVA_TOKEN", "credentials/canva-token.json").strip()
    token_path = Path(token_setting)
    if not token_path.is_absolute():
        token_path = project_root / token_path
    if not client_id or not client_secret or not token_path.is_file():
        return None
    try:
        from media_publisher.sources.canva import CanvaClient

        return CanvaClient(
            client_id=client_id,
            client_secret=client_secret,
            token_path=token_path,
        )
    except Exception:
        return None


def prepared_thumbnail_is_missing(
    *,
    fields: dict[str, Any],
    drive_service: Any,
    project_root: Path | None,
    log: Callable[[str], None],
    log_prefix: str = "  prepared thumbnail:",
) -> bool | None:
    """Return True when prepared thumb is missing, False when found, None if unchecked."""
    if not has_original_video_thumbnail(fields):
        return None

    title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    video_format = video_format_from_type(fields.get(FIELD_TYPE))
    root = project_root or Path(__file__).resolve().parents[3]
    from media_publisher.sources.google_drive import GoogleDriveClient
    from media_publisher.sources.publish_media import has_prepared_publish_thumbnail

    override_root_folder_id = ""
    if os.getenv("DRIVE_URL", "").strip():
        from media_publisher.sources.drive_layout import resolve_overrides_folder_id

        try:
            override_root_folder_id = resolve_overrides_folder_id(
                GoogleDriveClient(drive_service),
            )
        except Exception as exc:
            print(f"WARN: could not resolve Overrides Drive folder: {exc}")
    thumbnails_subfolder = (
        os.getenv("PUBLISH_OVERRIDE_THUMBNAILS_SUBFOLDER", "Thumbnails").strip()
        or "Thumbnails"
    )
    published_subfolder_name = (
        os.getenv("CANVA_PUBLISHED_SUBFOLDER_NAME", "Published").strip() or "Published"
    )
    drive = GoogleDriveClient(drive_service)
    canva_client = _optional_canva_client(root)
    checked_any = bool(override_root_folder_id) or canva_client is not None
    if not checked_any:
        log(
            f"{log_prefix} skipped check "
            "(Drive override folder / Canva client unavailable)"
        )
        return None

    try:
        from media_publisher.sources.canva import canva_catalog_urls_from_client

        parent_url = os.getenv("CANVA_URL", "").strip()
        if not parent_url:
            raise RuntimeError("CANVA_URL is required")
        long_catalog_url, short_catalog_url = canva_catalog_urls_from_client(
            canva_client,
            parent_url,
        )
        prepared = has_prepared_publish_thumbnail(
            title=title,
            video_format=video_format,
            drive=drive,
            canva_client=canva_client,
            override_root_folder_id=override_root_folder_id,
            thumbnails_subfolder=thumbnails_subfolder,
            published_subfolder_name=published_subfolder_name,
            long_catalog_url=long_catalog_url,
            short_catalog_url=short_catalog_url,
        )
    except Exception as exc:
        log(f"{log_prefix} check failed ({exc})")
        return None

    if prepared:
        log(f"{log_prefix} found in Canva catalog or Drive Thumbnails")
        return False

    log(
        f"{log_prefix} missing in Canva catalog and Drive Thumbnails "
        f"for {title!r}"
    )
    return True


def _notify_if_missing_prepared_thumbnail(
    *,
    fields: dict[str, Any],
    drive_service: Any,
    target_date: date,
    dry_run: bool,
    log: Callable[[str], None],
    project_root: Path | None = None,
    docs_service: Any | None = None,
) -> bool:
    """Email when Original Video Thumbnail is set but Canva/Drive prepared thumb is missing."""
    missing = prepared_thumbnail_is_missing(
        fields=fields,
        drive_service=drive_service,
        project_root=project_root,
        log=log,
    )
    if missing is not True:
        return False

    title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    translated = _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED))
    from catalog_parser.drive_thumbnail import resolve_canva_design_drive_url
    from media_publisher.sources.tn_publish import resolve_tn_template_drive_url

    canva_design = resolve_canva_design_drive_url(
        drive_service,
        fields,
        docs_service=docs_service,
    )
    tn_template = resolve_tn_template_drive_url(drive_service, fields)

    if dry_run:
        log("  prepared thumbnail: would email notification (dry-run)")
        return False

    sent = send_missing_prepared_thumbnail_email(
        title=title,
        translated=translated,
        canva_design=canva_design,
        target_date=target_date,
        tn_template=tn_template,
    )
    if sent:
        log("  prepared thumbnail: notification email sent")
        return True
    log(
        "  prepared thumbnail: notification email not sent "
        "(check GMAIL_SMTP_* / NOTIFY_EMAIL)"
    )
    return False


def _record_has_any_publish_date(fields: dict[str, Any]) -> bool:
    return any(_field_text(fields.get(field)) for field in PLATFORM_DATE_FIELDS)


def _record_has_pending_for_date(fields: dict[str, Any], target_date: date) -> bool:
    for date_field, published_field in zip(
        PLATFORM_DATE_FIELDS, PLATFORM_PUBLISHED_FIELDS, strict=True
    ):
        if _parse_date_field(fields.get(date_field)) != target_date:
            continue
        if _field_text(fields.get(published_field)):
            continue
        return True
    return False


def _has_pending_matching_type(
    records: list[dict[str, Any]],
    *,
    target_date: date,
    desired_type: str,
) -> bool:
    want_short = desired_type != TYPE_VIDEO
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if not is_sync_done_status(fields.get(FIELD_STATUS)):
            continue
        if is_quote_record(fields):
            continue
        if not _record_has_pending_for_date(fields, target_date):
            continue
        fmt = video_format_from_type(fields.get(FIELD_TYPE))
        if want_short and fmt == "short_form":
            return True
        if not want_short and fmt == "post":
            return True
    return False


def _select_candidate(
    records: list[dict[str, Any]],
    *,
    desired_type: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if is_quote_record(fields):
            continue
        if not is_sync_done_status(fields.get(FIELD_STATUS)):
            continue
        if not has_video_name_translated(fields):
            continue
        if is_done_published_status(fields.get(FIELD_STATUS)):
            continue
        if _record_has_any_publish_date(fields):
            continue
        if desired_type == TYPE_VIDEO and not is_video_type(fields):
            continue
        if desired_type != TYPE_VIDEO and not is_reel_type(fields):
            continue
        candidates.append(record)

    if not candidates:
        return None

    def _sort_key(record: dict[str, Any]) -> tuple[int, str]:
        fields = record.get("fields", {})
        thumb_rank = 0 if has_original_video_thumbnail(fields) else 1
        created = record.get("createdTime") or ""
        return (thumb_rank, str(created))

    candidates.sort(key=_sort_key)
    return candidates[0]


def _build_schedule_fields(fields: dict[str, Any], target_date: date) -> dict[str, str]:
    update_fields = {
        FIELD_SG_YT_DATE: _iso_date(target_date),
        FIELD_SG_FB_DATE: _iso_date(target_date),
    }
    if not instagram_schedule_excluded(fields):
        update_fields[FIELD_SG_IG_DATE] = _iso_date(target_date)
    return update_fields


def schedule_tomorrow_publish(
    *,
    airtable: AirtableClient,
    records: list[dict[str, Any]],
    drive_service: Any,
    dry_run: bool = False,
    target_date: date | None = None,
    log: Callable[[str], None] | None = None,
    project_root: Path | None = None,
    docs_service: Any | None = None,
) -> ScheduleTomorrowResult:
    """Pick one catalog record for tomorrow and set SG publish dates."""
    from zoneinfo import ZoneInfo

    publish_timezone, _publish_hour = _publish_settings()
    if target_date is None:
        today_local = datetime.now(ZoneInfo(publish_timezone)).date()
        target_date = today_local + timedelta(days=1)

    emit = log or (lambda _message: None)
    desired = desired_type_for_publish_date(target_date)

    if _has_pending_matching_type(
        records,
        target_date=target_date,
        desired_type=desired,
    ):
        return ScheduleTomorrowResult(
            success=True,
            message=(
                f"No update needed for {target_date.isoformat()} "
                f"({desired} already scheduled)."
            ),
            target_date=target_date,
        )

    chosen = _select_candidate(records, desired_type=desired)
    if chosen is None:
        return ScheduleTomorrowResult(
            success=True,
            message=(
                f"No matching unscheduled {desired} record found "
                f"for {target_date.isoformat()}."
            ),
            target_date=target_date,
        )

    record_id = chosen.get("id")
    fields = chosen.get("fields")
    if not isinstance(record_id, str) or not record_id or not isinstance(fields, dict):
        return ScheduleTomorrowResult(
            success=False,
            message="Selected record is missing id or fields.",
            target_date=target_date,
        )

    title = _field_text(fields.get(FIELD_TITLE)) or "Untitled"
    update_fields = _build_schedule_fields(fields, target_date)

    if dry_run:
        emit(
            f"Publish schedule preview for {target_date.isoformat()}: "
            f"{record_id}\t{title}"
        )
        for label, value in update_fields.items():
            emit(f"  {label}: {value}")
        _notify_if_missing_prepared_thumbnail(
            fields=fields,
            drive_service=drive_service,
            target_date=target_date,
            dry_run=True,
            log=emit,
            project_root=project_root,
            docs_service=docs_service,
        )
        return ScheduleTomorrowResult(
            success=True,
            message=f"Preview scheduled {record_id} for {target_date.isoformat()}.",
            record_id=record_id,
            target_date=target_date,
            applied=False,
        )

    airtable.update_record_fields(record_id, update_fields)
    emit(
        f"Publish schedule applied for {target_date.isoformat()}: "
        f"{record_id}\t{title}"
    )
    for label, value in update_fields.items():
        emit(f"  {label}: {value}")

    # Keep Combined Media File until after successful publish; clearing it here
    # broke no-subtitle publishes that still need the file.
    notified = _notify_if_missing_prepared_thumbnail(
        fields=fields,
        drive_service=drive_service,
        target_date=target_date,
        dry_run=False,
        log=emit,
        project_root=project_root,
        docs_service=docs_service,
    )

    return ScheduleTomorrowResult(
        success=True,
        message=f"Scheduled {record_id} for {target_date.isoformat()}.",
        record_id=record_id,
        target_date=target_date,
        applied=True,
        missing_prepared_thumbnail_notified=notified,
    )
