"""Drive review queue and approved thumbnail processing for Original Video Thumbnail."""

from __future__ import annotations

import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import aspects_match

REVIEW_FILENAME_SUFFIX = ".review"
DEFAULT_APPROVED_SUBFOLDER = "Approved"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


@dataclass(frozen=True)
class ReviewQueueItem:
    record_id: str
    title: str
    local_path: Path
    reason: str


@dataclass(frozen=True)
class ApprovedUploadResult:
    title: str
    record_id: str
    drive_file: str
    action: str
    caption_action: str = "skipped"
    caption_detail: str | None = None


def _field_text(fields: dict[str, Any], key: str) -> str | None:
    value = fields.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _translate_caption_for_approved_thumbnail(
    airtable: AirtableClient,
    record: Any,
    local_path: Path,
    *,
    project_root: Path | None,
    drive_service: Any | None,
) -> tuple[str, str | None]:
    """Fill empty Video caption translated from the approved image (ingest parity).

    Returns ``(caption_action, detail)`` where action is translated / skipped / failed.
    """
    fields = record.fields if hasattr(record, "fields") else {}
    if not isinstance(fields, dict):
        fields = {}

    existing = _field_text(fields, FIELD_VIDEO_CAPTION_TRANSLATED)
    if existing:
        return "skipped", "caption already set"

    from catalog_parser.translation.caption_prefill import (
        translate_record_caption_if_needed,
    )

    catalog_record: dict[str, Any] = {
        "_originalThumbnailPath": str(local_path),
        "pkgLink": _field_text(fields, FIELD_VIDEO_FOLDER),
        "bgCaption": None,
    }
    try:
        result = translate_record_caption_if_needed(
            catalog_record,
            project_root=project_root,
            drive_service=drive_service,
        )
    except Exception as exc:  # noqa: BLE001 — approve must continue
        return "failed", str(exc)

    if result.caption_translated:
        bg = catalog_record.get("bgCaption")
        if not isinstance(bg, str) or not bg.strip():
            return "failed", "empty translation"
        try:
            airtable.update_record(
                record.id,
                {FIELD_VIDEO_CAPTION_TRANSLATED: bg.strip()},
            )
        except Exception as exc:  # noqa: BLE001 — approve must continue
            return "failed", f"airtable update: {exc}"
        source = result.source or "unknown"
        return "translated", f"source={source}"

    detail_parts = [error for error in result.errors if error]
    if result.skipped:
        return "skipped", "; ".join(detail_parts) if detail_parts else "skipped"
    return "failed", "; ".join(detail_parts) if detail_parts else "translate failed"


def sanitize_review_stem(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
    return cleaned or "thumbnail"


def write_manual_canva_review_placeholder(
    destination: Path,
    *,
    canva_url: str,
    size: tuple[int, int] | None = None,
) -> Path:
    """Write a review-queue JPG instructing manual Canva download."""
    from media_publisher.sources.canva import CanvaError, parse_design_id

    width, height = size if size and size[0] > 0 and size[1] > 0 else (1280, 720)
    width = max(640, min(int(width), 1920))
    height = max(360, min(int(height), 1920))

    try:
        design_id = parse_design_id(canva_url)
    except CanvaError:
        design_id = "unknown"
    lines = [
        "Download this design",
        "manually from Canva",
        "",
        design_id,
    ]

    destination.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (width, height), color=(32, 32, 36))
    from PIL import ImageDraw, ImageFont

    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", size=max(28, width // 28))
        small = ImageFont.truetype("arial.ttf", size=max(18, width // 40))
    except OSError:
        font = ImageFont.load_default()
        small = font

    y = height // 3
    for index, line in enumerate(lines):
        use_font = small if index >= 2 else font
        bbox = draw.textbbox((0, 0), line, font=use_font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = max(0, (width - text_w) // 2)
        draw.text((x, y), line, fill=(240, 240, 240), font=use_font)
        y += text_h + (18 if index < 2 else 10)

    image.save(destination, format="JPEG", quality=90)
    return destination


def review_drive_filename(title: str) -> str:
    return f"{sanitize_review_stem(title)}{REVIEW_FILENAME_SUFFIX}.jpg"


def title_from_review_filename(name: str) -> str | None:
    path = Path(name)
    if path.suffix.casefold() not in IMAGE_EXTENSIONS:
        return None
    stem = path.stem
    if not stem.endswith(REVIEW_FILENAME_SUFFIX):
        return None
    title_stem = stem[: -len(REVIEW_FILENAME_SUFFIX)]
    return title_stem or None


def thumbnail_matches_reference_aspect(
    image: Image.Image,
    *,
    reference_width: int,
    reference_height: int,
) -> bool:
    return aspects_match(
        image.size[0],
        image.size[1],
        reference_width,
        reference_height,
    )


def format_review_email(
    items: list[ReviewQueueItem],
    *,
    review_folder_url: str = "",
) -> tuple[str, str]:
    subject = f"Thumbnail review requested ({len(items)} video(s))"
    lines = [
        "The following catalog videos need Original Video Thumbnail review.",
        "",
        f"Review folder: {review_folder_url or os.getenv('DRIVE_URL', '').strip() or 'Thumbnails for approval'}",
        "",
        "Move approved thumbnails into the Approved subfolder.",
        "",
    ]
    for item in sorted(items, key=lambda row: row.title.casefold()):
        lines.append(f"- {item.title}")
        lines.append(f"  Reason: {item.reason}")
    body = "\n".join(lines).strip() + "\n"
    return subject, body


def send_review_notification_email(
    items: list[ReviewQueueItem],
    *,
    review_folder_url: str = "",
) -> bool:
    if not items:
        return False

    scripts_dir = Path(__file__).resolve().parents[3] / "scripts" / "catalog"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from send_notification_email import send_email

    smtp_user = os.getenv("GMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("GMAIL_SMTP_APP_PASSWORD", "").strip()
    notify_email = os.getenv("NOTIFY_EMAIL", "").strip()
    if not smtp_user or not smtp_password or not notify_email:
        return False

    subject, body = format_review_email(items, review_folder_url=review_folder_url)
    send_email(
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        to_address=notify_email,
        subject=subject,
        body=body,
    )
    return True


def upload_review_thumbnail(
    drive_service: Any,
    *,
    review_folder_id: str,
    local_path: Path,
    title: str,
) -> str:
    from catalog_parser.drive_combine import upload_drive_file

    uploaded = upload_drive_file(
        drive_service,
        review_folder_id,
        local_path,
        name=review_drive_filename(title),
        mime_type="image/jpeg",
    )
    return uploaded.id


def process_approved_review_thumbnails(
    airtable: AirtableClient,
    drive: GoogleDriveClient,
    records: list[Any],
    *,
    review_folder_id: str,
    approved_subfolder: str = DEFAULT_APPROVED_SUBFOLDER,
    apply: bool,
    project_root: Path | None = None,
    translate_captions: bool = True,
) -> list[ApprovedUploadResult]:
    approved_folder = drive.find_child_folder(review_folder_id, approved_subfolder)
    if approved_folder is None:
        return []

    records_by_stem = {
        sanitize_review_stem(catalog_title(record.fields)): record
        for record in records
    }
    results: list[ApprovedUploadResult] = []
    drive_service = getattr(drive, "drive_service", None)

    with tempfile.TemporaryDirectory(prefix="tn-approved-") as tmp:
        tmp_path = Path(tmp)
        for item in drive.list_children(approved_folder.id):
            if not item.mime_type.startswith("image/"):
                continue
            title_stem = title_from_review_filename(item.name)
            if title_stem is None:
                continue
            record = records_by_stem.get(title_stem)
            if record is None:
                continue

            title = catalog_title(record.fields)
            action = "planned-approved"
            caption_action = "skipped"
            caption_detail: str | None = "dry-run" if not apply else None
            if apply:
                local_path = tmp_path / item.name
                drive.download_file(item.id, local_path)
                airtable.upload_attachment(
                    record.id,
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                    local_path,
                )
                if translate_captions:
                    caption_action, caption_detail = (
                        _translate_caption_for_approved_thumbnail(
                            airtable,
                            record,
                            local_path,
                            project_root=project_root,
                            drive_service=drive_service,
                        )
                    )
                else:
                    caption_action, caption_detail = "skipped", "disabled"
                drive.remove_file(item.id)
                action = "uploaded-approved"

            results.append(
                ApprovedUploadResult(
                    title=title,
                    record_id=record.id,
                    drive_file=item.name,
                    action=action,
                    caption_action=caption_action,
                    caption_detail=caption_detail,
                )
            )
    return results
