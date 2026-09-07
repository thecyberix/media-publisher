"""Backfill Airtable Original Video Thumbnail from package Canva designs."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    WORKFLOW_STATUSES,
)
from catalog_parser.canva import CanvaClient, is_canva_auth_error
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_thumbnail import (
    discover_package_canva_url,
    download_canva_thumbnail,
)
from media_publisher.sources.airtable import has_original_video_thumbnail


@dataclass
class CanvaThumbBackfillItem:
    record_id: str
    title: str
    status: str
    video_type: str
    had_thumbnail: bool
    had_caption: bool
    canva_url: str | None = None
    action: str = "pending"
    detail: str | None = None
    caption_action: str = "pending"
    caption_detail: str | None = None

    @property
    def modified(self) -> bool:
        return self.action == "uploaded" or self.caption_action == "translated"


@dataclass
class CanvaThumbBackfillResult:
    checked: int = 0
    with_canva: int = 0
    uploaded: int = 0
    would_upload: int = 0
    captions_translated: int = 0
    captions_would_translate: int = 0
    skipped: int = 0
    failed: int = 0
    items: list[CanvaThumbBackfillItem] = field(default_factory=list)

    @property
    def modified_items(self) -> list[CanvaThumbBackfillItem]:
        return [item for item in self.items if item.modified]


def _workflow_status_formula() -> str:
    clauses = [f'{{Status}}="{status}"' for status in WORKFLOW_STATUSES]
    return f"OR({', '.join(clauses)})"


def _safe_filename(title: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in title)
    cleaned = cleaned.strip("._") or "thumbnail"
    return f"{cleaned[:80]}.canva.jpg"


def _field_text(fields: dict[str, Any], name: str) -> str | None:
    value = fields.get(name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _translate_caption_if_missing(
    *,
    airtable: Any,
    record_id: str,
    fields: dict[str, Any],
    local_path: Path,
    drive_service: Any,
    project_root: Path | None,
) -> tuple[str, str | None]:
    """Fill empty Video caption translated from the Canva thumbnail image."""
    if _field_text(fields, FIELD_VIDEO_CAPTION_TRANSLATED):
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
    except Exception as exc:  # noqa: BLE001 — backfill must continue
        return "failed", str(exc)

    if result.caption_translated:
        bg = catalog_record.get("bgCaption")
        if not isinstance(bg, str) or not bg.strip():
            return "failed", "empty translation"
        try:
            airtable.update_record_fields(
                record_id,
                {FIELD_VIDEO_CAPTION_TRANSLATED: bg.strip()},
            )
        except Exception as exc:  # noqa: BLE001 — backfill must continue
            return "failed", f"airtable update: {exc}"
        source = result.source or "unknown"
        return "translated", f"source={source}"

    detail_parts = [error for error in result.errors if error]
    if result.skipped:
        return "skipped", "; ".join(detail_parts) if detail_parts else "skipped"
    return "failed", "; ".join(detail_parts) if detail_parts else "translate failed"


def backfill_canva_thumbnails(
    *,
    airtable: Any,
    drive_service: Any,
    docs_service: Any | None,
    canva_client: CanvaClient,
    dry_run: bool = True,
    project_root: Path | None = None,
    log: Callable[[str], None] = print,
) -> CanvaThumbBackfillResult:
    """Replace Original Video Thumbnail from package Canva for workflow statuses.

    Includes rows that already have a thumbnail — prior ingest may have used the
    wrong (platform) image when Canva short links were missed. When
    ``Video caption translated`` is empty, also AI-translate from the Canva image.
    """
    records = airtable.list_records(filter_formula=_workflow_status_formula())
    result = CanvaThumbBackfillResult(checked=len(records))
    log(f"Loaded {len(records)} workflow-status record(s)")

    for index, record in enumerate(records, start=1):
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        record_id = str(record.get("id") or "")
        title = str(fields.get(FIELD_TITLE) or record_id or f"row-{index}")
        had_caption = _field_text(fields, FIELD_VIDEO_CAPTION_TRANSLATED) is not None
        item = CanvaThumbBackfillItem(
            record_id=record_id,
            title=title,
            status=str(fields.get(FIELD_STATUS) or ""),
            video_type=str(fields.get(FIELD_TYPE) or ""),
            had_thumbnail=has_original_video_thumbnail(fields),
            had_caption=had_caption,
        )

        folder_link = fields.get(FIELD_VIDEO_FOLDER)
        if not isinstance(folder_link, str) or not folder_link.strip():
            item.action = "skipped"
            item.detail = "missing Video Folder"
            item.caption_action = "skipped"
            item.caption_detail = "no package"
            result.skipped += 1
            result.items.append(item)
            continue
        if extract_drive_folder_id(folder_link) is None:
            item.action = "skipped"
            item.detail = "unparseable Video Folder"
            item.caption_action = "skipped"
            item.caption_detail = "no package"
            result.skipped += 1
            result.items.append(item)
            continue

        try:
            canva_url = discover_package_canva_url(
                drive_service,
                docs_service,
                fields,
            )
        except Exception as exc:  # noqa: BLE001
            item.action = "failed"
            item.detail = f"canva discovery: {exc}"
            item.caption_action = "skipped"
            item.caption_detail = "canva discovery failed"
            result.failed += 1
            result.items.append(item)
            log(f"FAIL {title}: {item.detail}")
            continue

        if not canva_url:
            item.action = "skipped"
            item.detail = "no Canva link in package"
            item.caption_action = "skipped"
            item.caption_detail = "no Canva link"
            result.skipped += 1
            result.items.append(item)
            continue

        item.canva_url = canva_url
        result.with_canva += 1
        replace_note = "replace" if item.had_thumbnail else "set"
        caption_note = (
            "caption already set"
            if had_caption
            else "would translate caption"
            if dry_run
            else "translate caption if missing"
        )

        if dry_run:
            item.action = "would_upload"
            item.detail = f"{replace_note} from {canva_url}"
            item.caption_action = "would_translate" if not had_caption else "skipped"
            item.caption_detail = caption_note
            result.would_upload += 1
            if not had_caption:
                result.captions_would_translate += 1
            result.items.append(item)
            log(f"WOULD {replace_note} [{item.status}] {title}")
            log(f"  {canva_url}")
            log(f"  caption: {item.caption_action} ({item.caption_detail})")
            continue

        try:
            with tempfile.TemporaryDirectory(prefix="canva-thumb-") as tmp:
                destination = Path(tmp) / _safe_filename(title)
                source = download_canva_thumbnail(
                    canva_url,
                    destination,
                    canva_client=canva_client,
                )
                airtable.upload_attachment(
                    record_id,
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                    destination,
                    replace=True,
                )
                item.action = "uploaded"
                item.detail = f"{replace_note} via {source}"
                result.uploaded += 1
                log(f"OK {replace_note} [{item.status}] {title}")
                log(f"  {item.detail} | {canva_url}")

                caption_action, caption_detail = _translate_caption_if_missing(
                    airtable=airtable,
                    record_id=record_id,
                    fields=fields,
                    local_path=destination,
                    drive_service=drive_service,
                    project_root=project_root,
                )
                item.caption_action = caption_action
                item.caption_detail = caption_detail
                if caption_action == "translated":
                    result.captions_translated += 1
                log(f"  caption: {caption_action}" + (f" ({caption_detail})" if caption_detail else ""))
        except Exception as exc:  # noqa: BLE001
            if is_canva_auth_error(exc):
                raise
            item.action = "failed"
            item.detail = str(exc)
            item.caption_action = "skipped"
            item.caption_detail = "thumbnail upload failed"
            result.failed += 1
            log(f"FAIL [{item.status}] {title}: {exc}")
        result.items.append(item)

    modified = result.modified_items
    log(
        "Done: "
        f"checked={result.checked} with_canva={result.with_canva} "
        f"uploaded={result.uploaded} would_upload={result.would_upload} "
        f"captions_translated={result.captions_translated} "
        f"captions_would_translate={result.captions_would_translate} "
        f"skipped={result.skipped} failed={result.failed}"
    )
    log(f"Modified Airtable entries: {len(modified)}")
    for item in modified:
        log(
            f"  - [{item.status}] {item.title} "
            f"(thumb={item.action}; caption={item.caption_action}"
            f"{f'/{item.caption_detail}' if item.caption_detail else ''})"
        )
    return result
