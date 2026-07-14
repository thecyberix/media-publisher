from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable

from googleapiclient.errors import HttpError

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_DURATION,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_SYNC_DONE,
)
from catalog_parser.drive_docs import extract_drive_file_id
from catalog_parser.parser import TYPE_REEL, TYPE_SHORT, TYPE_VIDEO, parse_duration

DEFAULT_PUBLISH_TIMEZONE = "Europe/Sofia"
DEFAULT_PUBLISH_HOUR = 18
MAX_INSTAGRAM_VIDEO_SECONDS = 15 * 60

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


def _publish_settings() -> tuple[str, int]:
    timezone = os.getenv("PUBLISH_TIMEZONE", DEFAULT_PUBLISH_TIMEZONE).strip()
    if not timezone:
        timezone = DEFAULT_PUBLISH_TIMEZONE
    hour_raw = os.getenv("PUBLISH_HOUR", str(DEFAULT_PUBLISH_HOUR)).strip()
    try:
        publish_hour = int(hour_raw or str(DEFAULT_PUBLISH_HOUR))
    except ValueError:
        publish_hour = DEFAULT_PUBLISH_HOUR
    return timezone, publish_hour


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
    duration = parse_duration(fields.get(FIELD_DURATION))
    return duration is not None and duration > MAX_INSTAGRAM_VIDEO_SECONDS


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


def _file_capabilities(drive_service: Any, file_id: str) -> dict[str, bool]:
    try:
        metadata = (
            drive_service.files()
            .get(
                fileId=file_id,
                fields="capabilities(canDelete,canTrash)",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError:
        return {}
    capabilities = metadata.get("capabilities")
    if not isinstance(capabilities, dict):
        return {}
    return {
        "canDelete": bool(capabilities.get("canDelete")),
        "canTrash": bool(capabilities.get("canTrash")),
    }


def _remove_drive_file(drive_service: Any, file_id: str) -> str:
    capabilities = _file_capabilities(drive_service, file_id)
    if capabilities.get("canDelete"):
        drive_service.files().delete(
            fileId=file_id,
            supportsAllDrives=True,
        ).execute()
        return "deleted"
    if capabilities.get("canTrash"):
        drive_service.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
        return "trashed"
    raise RuntimeError(
        f"Drive file {file_id} cannot be deleted or trashed with current permissions"
    )


def _clear_combined_media_file(
    *,
    drive_service: Any,
    airtable: AirtableClient,
    record_id: str,
    fields: dict[str, Any],
    dry_run: bool,
    log: Callable[[str], None],
) -> bool:
    combined = fields.get(FIELD_COMBINED_MEDIA_FILE)
    if combined is None:
        return True
    if not isinstance(combined, str):
        combined = str(combined)
    if not combined.strip():
        return True

    file_id = extract_drive_file_id(combined)
    if not file_id:
        log(f"  combined media: could not parse Drive id from {combined!r}")
        return False

    if dry_run:
        log(
            f"  combined media: would remove Drive file {file_id} "
            f"and clear {FIELD_COMBINED_MEDIA_FILE!r}"
        )
        return True

    try:
        action = _remove_drive_file(drive_service, file_id)
        log(f"  combined media: {action} Drive file {file_id}")
    except Exception as exc:
        log(f"  combined media: failed to remove Drive file {file_id}: {exc}")
        return False

    airtable.update_record_fields(record_id, {FIELD_COMBINED_MEDIA_FILE: ""})
    log(f"  combined media: cleared {FIELD_COMBINED_MEDIA_FILE!r} on {record_id}")
    return True


def schedule_tomorrow_publish(
    *,
    airtable: AirtableClient,
    records: list[dict[str, Any]],
    drive_service: Any,
    dry_run: bool = False,
    target_date: date | None = None,
    log: Callable[[str], None] | None = None,
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
        _clear_combined_media_file(
            drive_service=drive_service,
            airtable=airtable,
            record_id=record_id,
            fields=fields,
            dry_run=True,
            log=emit,
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

    combined_ok = _clear_combined_media_file(
        drive_service=drive_service,
        airtable=airtable,
        record_id=record_id,
        fields=fields,
        dry_run=False,
        log=emit,
    )
    if not combined_ok:
        return ScheduleTomorrowResult(
            success=False,
            message=(
                f"Scheduled {record_id} for {target_date.isoformat()}, "
                "but combined media cleanup failed."
            ),
            record_id=record_id,
            target_date=target_date,
            applied=True,
        )

    return ScheduleTomorrowResult(
        success=True,
        message=f"Scheduled {record_id} for {target_date.isoformat()}.",
        record_id=record_id,
        target_date=target_date,
        applied=True,
    )
