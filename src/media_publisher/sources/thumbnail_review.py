"""Drive review queue and approved thumbnail processing for Original Video Thumbnail."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
DEFAULT_REJECTED_SUBFOLDER = "Rejected"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
BACKGROUND_REVIEW_DECISIONS = frozenset(
    {"approve", "reject", "empty", "placeholder"}
)
BACKGROUND_REVIEW_CLASSIFY_PROMPT = (
    "Classify this original video thumbnail background image.\n"
    "Decide exactly one of:\n"
    '- "approve": the image includes a title or headline overlay '
    "(large designed title text), with or without smaller caption text.\n"
    '- "reject": the image has overlay text that is only subtitle/caption style '
    "(typically smaller lines near the bottom) and does not include a title/headline.\n"
    '- "empty": there is no meaningful overlay text (photo-only / empty background).\n'
    '- "placeholder": the image is an instructional Canva download placeholder '
    '(for example "Download this design manually from Canva").\n'
    'Return ONLY JSON: {"decision": "approve"|"reject"|"empty"|"placeholder", '
    '"reason": "short"}.\n'
)


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


@dataclass(frozen=True)
class PendingReviewSortResult:
    drive_file: str
    decision: str
    action: str
    reason: str = ""


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
        "Move approved thumbnails (with a title) into the Approved subfolder.",
        "Move subtitle-only and empty backgrounds into Rejected when that folder exists.",
        "Leave Canva download placeholders in this folder.",
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


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def parse_background_review_decision(raw: str) -> tuple[str, str]:
    """Return ``(decision, reason)``; unknown output stays as placeholder."""
    cleaned = _strip_json_fence(raw)
    payload: Any = None
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                payload = None
    if isinstance(payload, dict):
        decision = str(payload.get("decision") or "").strip().casefold()
        reason = str(payload.get("reason") or "").strip()
        if decision == "skip":
            return "placeholder", reason or "skip"
        if decision in BACKGROUND_REVIEW_DECISIONS:
            return decision, reason
    lowered = cleaned.casefold()
    for decision in ("placeholder", "approve", "reject", "empty"):
        if f'"{decision}"' in lowered or f" {decision}" in lowered:
            return decision, "parsed from unstructured response"
    return "placeholder", "unrecognized classifier output"


def classify_original_background_thumbnail(
    path: Path,
    *,
    config: Any | None = None,
) -> tuple[str, str]:
    """Vision-classify a review-queue original: approve, reject, empty, or placeholder."""
    from catalog_parser.translation.rag_translate import (
        chat_completion_with_image,
        chat_config_from_env,
    )

    chat_config = config if config is not None else chat_config_from_env()
    raw_bytes = path.read_bytes()
    suffix = path.suffix.casefold()
    media_type = "image/jpeg"
    if suffix == ".png":
        media_type = "image/png"
    elif suffix == ".webp":
        media_type = "image/webp"
    raw = chat_completion_with_image(
        BACKGROUND_REVIEW_CLASSIFY_PROMPT,
        raw_bytes,
        chat_config,
        media_type=media_type,
    )
    return parse_background_review_decision(raw)


def process_pending_review_thumbnails(
    drive: GoogleDriveClient,
    *,
    review_folder_id: str,
    approved_subfolder: str = DEFAULT_APPROVED_SUBFOLDER,
    apply: bool,
    classify: Callable[[Path], tuple[str, str]] | None = None,
) -> list[PendingReviewSortResult]:
    """Sort review-queue originals; keep only Canva placeholders in the review folder."""
    classify_fn = classify or classify_original_background_thumbnail
    results: list[PendingReviewSortResult] = []
    approved_folder = None
    rejected_folder = drive.find_child_folder(
        review_folder_id, DEFAULT_REJECTED_SUBFOLDER
    )

    with tempfile.TemporaryDirectory(prefix="tn-review-sort-") as tmp:
        tmp_path = Path(tmp)
        for item in drive.list_children(review_folder_id):
            if not item.mime_type.startswith("image/"):
                continue
            if title_from_review_filename(item.name) is None:
                continue
            local_path = tmp_path / item.name
            drive.download_file(item.id, local_path)
            try:
                decision, reason = classify_fn(local_path)
            except Exception as exc:  # noqa: BLE001 — leave Canva placeholders in queue
                decision, reason = "placeholder", f"classifier error: {exc}"
            if decision == "skip":
                decision = "placeholder"
            if decision not in BACKGROUND_REVIEW_DECISIONS:
                decision, reason = "placeholder", reason or "invalid decision"

            if decision == "placeholder":
                results.append(
                    PendingReviewSortResult(
                        drive_file=item.name,
                        decision="placeholder",
                        action="kept" if apply else "planned-keep",
                        reason=reason or "Canva placeholder",
                    )
                )
                continue

            if decision == "approve":
                action = "planned-approve"
                if apply:
                    if approved_folder is None:
                        approved_folder = drive.ensure_folder(
                            review_folder_id, approved_subfolder
                        )
                    drive.move_file(item.id, approved_folder.id)
                    action = "moved-approved"
                results.append(
                    PendingReviewSortResult(
                        drive_file=item.name,
                        decision="approve",
                        action=action,
                        reason=reason,
                    )
                )
                continue

            action = f"planned-{decision}"
            if apply:
                if rejected_folder is None:
                    action = "skipped-rejected-folder-missing"
                else:
                    drive.move_file(item.id, rejected_folder.id)
                    action = "moved-rejected"
            elif rejected_folder is None:
                action = "planned-skip-rejected-folder-missing"
            results.append(
                PendingReviewSortResult(
                    drive_file=item.name,
                    decision=decision,
                    action=action,
                    reason=reason,
                )
            )
    return results


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
