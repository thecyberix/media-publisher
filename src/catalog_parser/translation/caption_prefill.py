"""Ingest-time AI translation of Bulgarian video caption from thumbnail / Drive TN."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from catalog_parser.drive_docs import (
    GOOGLE_DOC_MIME_TYPE,
    WORD_DOC_MIME_TYPE,
    extract_drive_folder_id,
    list_text_documents_in_folder,
)
from catalog_parser.translation.index import (
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_METADATA_PAIRS_PATH,
    DEFAULT_METADATA_TITLE_INDEX_PATH,
)
from catalog_parser.translation.metadata_prefill import get_metadata_index
from catalog_parser.translation.prefill import ai_prefill_enabled
from catalog_parser.translation.rag_translate import (
    DEFAULT_METADATA_TOP_K,
    ChatConfig,
    chat_config_from_env,
    extract_caption_lines_from_image_path,
    translate_metadata_field,
)
from googleapiclient.discovery import Resource
from media_publisher.sources.tn_docx import (
    TN_LABEL,
    english_lines_for_render,
    extract_labeled_table,
    extract_tn_text,
)


@dataclass
class CaptionTranslateResult:
    ok: bool
    caption_translated: bool = False
    skipped: bool = False
    source: str | None = None  # "thumbnail" | "drive_tn" | None
    errors: list[str] = field(default_factory=list)


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _thumbnail_path_from_record(record: dict[str, Any]) -> Path | None:
    raw = record.get("_originalThumbnailPath")
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(raw.strip())
    if path.is_file():
        return path
    return None


def extract_english_caption_from_thumbnail(
    record: dict[str, Any],
    *,
    config: ChatConfig,
) -> list[str]:
    path = _thumbnail_path_from_record(record)
    if path is None:
        return []
    return extract_caption_lines_from_image_path(path, config)


def _load_docx_from_drive_file(
    drive_service: Resource,
    document: dict[str, str],
) -> Any | None:
    from docx import Document

    document_id = document.get("id")
    mime_type = document.get("mimeType")
    if not document_id or not mime_type:
        return None
    try:
        if mime_type == WORD_DOC_MIME_TYPE:
            content = drive_service.files().get_media(fileId=document_id).execute()
            return Document(io.BytesIO(content))
        if mime_type == GOOGLE_DOC_MIME_TYPE:
            content = (
                drive_service.files()
                .export_media(fileId=document_id, mimeType=WORD_DOC_MIME_TYPE)
                .execute()
            )
            return Document(io.BytesIO(content))
    except Exception:
        return None
    return None


def extract_english_caption_from_drive_tn(
    drive_service: Resource | None,
    folder_id: str | None,
) -> list[str]:
    if drive_service is None or not folder_id:
        return []
    try:
        documents = list_text_documents_in_folder(drive_service, folder_id)
    except Exception:
        return []
    for document in documents:
        docx = _load_docx_from_drive_file(drive_service, document)
        if docx is None:
            continue
        grid = extract_labeled_table(docx, TN_LABEL)
        if grid is None:
            continue
        english = extract_tn_text(grid).get("english")
        if isinstance(english, str) and english.strip():
            return english_lines_for_render(english)
    return []


def resolve_english_caption_lines(
    record: dict[str, Any],
    *,
    config: ChatConfig,
    drive_service: Resource | None = None,
) -> tuple[list[str], str | None]:
    """Return (lines, source) with thumbnail vision first, Drive TN fallback."""
    try:
        vision_lines = extract_english_caption_from_thumbnail(record, config=config)
    except Exception:
        vision_lines = []
    if vision_lines:
        return vision_lines, "thumbnail"

    folder_link = record.get("pkgLink")
    folder_id = (
        extract_drive_folder_id(folder_link)
        if isinstance(folder_link, str)
        else None
    )
    drive_lines = extract_english_caption_from_drive_tn(drive_service, folder_id)
    if drive_lines:
        return drive_lines, "drive_tn"
    return [], None


def translate_record_caption_if_needed(
    record: dict[str, Any],
    *,
    project_root: Path | None = None,
    config: ChatConfig | None = None,
    top_k: int = DEFAULT_METADATA_TOP_K,
    pairs_path: Path = DEFAULT_METADATA_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    title_index_path: Path = DEFAULT_METADATA_TITLE_INDEX_PATH,
    drive_service: Resource | None = None,
    enabled: bool | None = None,
) -> CaptionTranslateResult:
    """
    Translate EN caption into bgCaption on the record.

    Gated by SMARTCAT_AI_PREFILL unless ``enabled`` is passed explicitly.
    """
    if enabled is None:
        enabled = ai_prefill_enabled()
    if not enabled:
        return CaptionTranslateResult(ok=True, skipped=True)

    if _text_or_none(record.get("bgCaption")):
        return CaptionTranslateResult(ok=True, skipped=True)

    chat = config or chat_config_from_env()
    try:
        lines, source = resolve_english_caption_lines(
            record,
            config=chat,
            drive_service=drive_service,
        )
    except Exception as exc:  # noqa: BLE001 — ingest must continue
        return CaptionTranslateResult(
            ok=False,
            skipped=True,
            errors=[f"caption resolve: {exc}"],
        )

    if not lines:
        return CaptionTranslateResult(
            ok=True,
            skipped=True,
            errors=["no English caption from thumbnail or Drive TN"],
        )

    en_caption = "\n".join(lines)
    try:
        index = get_metadata_index(
            "title",
            project_root=project_root,
            pairs_path=pairs_path,
            holdout_path=holdout_path,
            title_index_path=title_index_path,
        )
        record["bgCaption"] = translate_metadata_field(
            en_caption,
            kind="caption",
            index=index,
            config=chat,
            top_k=top_k,
        )
        record["_captionSource"] = source
        return CaptionTranslateResult(
            ok=True,
            caption_translated=True,
            source=source,
        )
    except Exception as exc:  # noqa: BLE001 — ingest must continue
        return CaptionTranslateResult(
            ok=False,
            skipped=False,
            source=source,
            errors=[f"caption: {exc}"],
        )
