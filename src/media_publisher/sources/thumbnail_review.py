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
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import aspects_match

REVIEW_FILENAME_SUFFIX = ".review"
DEFAULT_REVIEW_FOLDER_ID = "1lSr2x3xguVbqjBbOQN2bOR3Vbn-xhCIN"
DEFAULT_APPROVED_SUBFOLDER = "Approved"
DEFAULT_REVIEW_FOLDER_URL = (
    "https://drive.google.com/drive/u/1/folders/1lSr2x3xguVbqjBbOQN2bOR3Vbn-xhCIN"
)
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


def sanitize_review_stem(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
    return cleaned or "thumbnail"


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
    review_folder_url: str = DEFAULT_REVIEW_FOLDER_URL,
) -> tuple[str, str]:
    subject = f"Thumbnail review requested ({len(items)} video(s))"
    lines = [
        "The following catalog videos need Original Video Thumbnail review.",
        "",
        f"Review folder: {review_folder_url}",
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
    review_folder_url: str = DEFAULT_REVIEW_FOLDER_URL,
) -> bool:
    if not items:
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
) -> list[ApprovedUploadResult]:
    approved_folder = drive.find_child_folder(review_folder_id, approved_subfolder)
    if approved_folder is None:
        return []

    records_by_stem = {
        sanitize_review_stem(catalog_title(record.fields)): record
        for record in records
    }
    results: list[ApprovedUploadResult] = []

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
            if apply:
                local_path = tmp_path / item.name
                drive.download_file(item.id, local_path)
                airtable.upload_attachment(
                    record.id,
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                    local_path,
                )
                drive.remove_file(item.id)
                action = "uploaded-approved"

            results.append(
                ApprovedUploadResult(
                    title=title,
                    record_id=record.id,
                    drive_file=item.name,
                    action=action,
                )
            )
    return results
