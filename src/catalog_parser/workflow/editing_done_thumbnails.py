"""Notify when videos enter Editing done and still need a prepared thumbnail."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from catalog_parser.airtable import (
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.publish_schedule import (
    FIELD_CANVA_DESIGN,
    append_template_links,
    has_original_video_thumbnail,
    prepared_thumbnail_is_missing,
)
from catalog_parser.workflow.table_cache import DEFAULT_BACKUP_DIR, TableCache


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class EditingDoneThumbCandidate:
    record_id: str
    title: str
    translated: str | None
    canva_design: str | None
    tn_template: str | None


@dataclass(frozen=True)
class EditingDoneThumbNotifyResult:
    checked: int
    missing: int
    emailed: bool
    message: str


def is_editing_done_status(value: Any) -> bool:
    text = _field_text(value)
    return text == STATUS_EDITING_DONE


def detect_newly_editing_done_records(
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return current records that newly entered Editing done since the previous snapshot."""
    previous_by_id = {
        record_id: record
        for record in previous_records
        if isinstance((record_id := record.get("id")), str)
    }
    newly_done: list[dict[str, Any]] = []
    for current in current_records:
        record_id = current.get("id")
        fields = current.get("fields")
        if not isinstance(record_id, str) or not isinstance(fields, dict):
            continue
        record_type = fields.get(FIELD_TYPE)
        if record_type not in {TYPE_VIDEO, TYPE_REEL}:
            continue
        if not is_editing_done_status(fields.get(FIELD_STATUS)):
            continue
        previous = previous_by_id.get(record_id)
        previous_fields = previous.get("fields") if isinstance(previous, dict) else None
        if not isinstance(previous_fields, dict):
            previous_fields = {}
        if is_editing_done_status(previous_fields.get(FIELD_STATUS)):
            continue
        newly_done.append(current)
    return newly_done


def format_editing_done_missing_prepared_thumbnails_email(
    candidates: list[EditingDoneThumbCandidate],
) -> tuple[str, str]:
    count = len(candidates)
    subject = f"Editing done — {count} video(s) need prepared thumbnail"
    lines = [
        "These videos entered Editing done with an Original Video Thumbnail,",
        "but no prepared thumbnail was found in the Canva catalog or Drive",
        "Thumbnails folder.",
        "",
        f"Count: {count}",
        "",
    ]
    for index, item in enumerate(candidates, start=1):
        lines.append(f"{index}. {item.title}")
        if item.translated:
            lines.append(f"   Translated: {item.translated}")
        append_template_links(
            lines,
            canva_design=item.canva_design,
            tn_template=item.tn_template,
        )
        lines.append("")
    return subject, "\n".join(lines).rstrip() + "\n"


def send_editing_done_missing_prepared_thumbnails_email(
    candidates: list[EditingDoneThumbCandidate],
) -> bool:
    if not candidates:
        return False

    scripts_dir = Path(__file__).resolve().parents[3] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from send_notification_email import send_email

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    notify_email = os.getenv("NOTIFY_EMAIL", "georgi.uzunov-ext@sadhguru.org").strip()
    if not smtp_user or not smtp_password or not notify_email:
        return False

    subject, body = format_editing_done_missing_prepared_thumbnails_email(candidates)
    send_email(
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_address=notify_email,
        subject=subject,
        body=body,
    )
    return True


def collect_editing_done_missing_prepared_thumbnails(
    *,
    records: list[dict[str, Any]],
    drive_service: Any,
    project_root: Path,
    log: Callable[[str], None] = print,
) -> list[EditingDoneThumbCandidate]:
    from media_publisher.sources.tn_publish import resolve_tn_template_drive_url

    candidates: list[EditingDoneThumbCandidate] = []
    for record in records:
        fields = record.get("fields")
        record_id = record.get("id")
        if not isinstance(fields, dict) or not isinstance(record_id, str):
            continue
        if not has_original_video_thumbnail(fields):
            continue
        missing = prepared_thumbnail_is_missing(
            fields=fields,
            drive_service=drive_service,
            project_root=project_root,
            log=log,
            log_prefix="  editing-done prepared thumbnail:",
        )
        if missing is not True:
            continue
        candidates.append(
            EditingDoneThumbCandidate(
                record_id=record_id,
                title=_field_text(fields.get(FIELD_TITLE)) or record_id,
                translated=_field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)),
                canva_design=_field_text(fields.get(FIELD_CANVA_DESIGN)),
                tn_template=resolve_tn_template_drive_url(drive_service, fields),
            )
        )
    return candidates


def notify_editing_done_missing_prepared_thumbnails(
    *,
    project_root: Path,
    current_records: list[dict[str, Any]],
    drive_service: Any,
    dry_run: bool = False,
    log: Callable[[str], None] = print,
    previous_records: list[dict[str, Any]] | None = None,
) -> EditingDoneThumbNotifyResult:
    """Email a digest for newly Editing-done videos that still need prepared thumbs."""
    if previous_records is None:
        previous_path = project_root / DEFAULT_BACKUP_DIR / "airtable-previous.json"
        if not previous_path.is_file():
            return EditingDoneThumbNotifyResult(
                checked=0,
                missing=0,
                emailed=False,
                message="No previous Airtable backup; skipped Editing-done thumbnail notify",
            )
        try:
            previous_records = TableCache.from_backup_file(previous_path).records
        except ValueError as exc:
            return EditingDoneThumbNotifyResult(
                checked=0,
                missing=0,
                emailed=False,
                message=f"Could not load previous Airtable backup: {exc}",
            )

    newly_done = detect_newly_editing_done_records(previous_records, current_records)
    if not newly_done:
        return EditingDoneThumbNotifyResult(
            checked=0,
            missing=0,
            emailed=False,
            message="No newly Editing-done videos",
        )

    log(f"Editing-done thumbnail check: {len(newly_done)} newly Editing-done video(s)")
    candidates = collect_editing_done_missing_prepared_thumbnails(
        records=newly_done,
        drive_service=drive_service,
        project_root=project_root,
        log=log,
    )
    if not candidates:
        return EditingDoneThumbNotifyResult(
            checked=len(newly_done),
            missing=0,
            emailed=False,
            message=(
                f"Checked {len(newly_done)} newly Editing-done video(s); "
                "none missing prepared thumbnails"
            ),
        )

    if dry_run:
        return EditingDoneThumbNotifyResult(
            checked=len(newly_done),
            missing=len(candidates),
            emailed=False,
            message=(
                f"Would email {len(candidates)} Editing-done video(s) "
                "missing prepared thumbnails (dry-run)"
            ),
        )

    sent = send_editing_done_missing_prepared_thumbnails_email(candidates)
    if sent:
        return EditingDoneThumbNotifyResult(
            checked=len(newly_done),
            missing=len(candidates),
            emailed=True,
            message=(
                f"Emailed {len(candidates)} Editing-done video(s) "
                "missing prepared thumbnails"
            ),
        )
    return EditingDoneThumbNotifyResult(
        checked=len(newly_done),
        missing=len(candidates),
        emailed=False,
        message=(
            f"Found {len(candidates)} Editing-done video(s) missing prepared thumbnails, "
            "but email was not sent (check GMAIL_SMTP_* / NOTIFY_EMAIL)"
        ),
    )
