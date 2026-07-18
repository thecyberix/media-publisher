"""Ingest-time AI prefill of Bulgarian Smartcat targets from English source."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_parser.smartcat import SmartcatError
from catalog_parser.smartcat_export import (
    WEB_EXPORT_TYPE_SOURCE,
    WEB_SEGMENT_EXPORT_MODE_SOURCE,
    SmartcatDocumentContext,
    SmartcatWebRequestClient,
    export_document_srt_via_web_api,
)
from catalog_parser.smartcat_write import SmartcatWebSrtImporter
from catalog_parser.translation.index import (
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_INDEX_PATH,
    DEFAULT_PAIRS_PATH,
    load_or_build_index,
)
from catalog_parser.translation.rag_translate import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_TOP_K,
    chat_config_from_env,
    translate_srt_text,
)
from catalog_parser.translation.srt import Cue, parse_srt, write_srt


@dataclass(frozen=True)
class PrefillResult:
    ok: bool
    source_cues: int = 0
    written_segments: int = 0
    error: str | None = None
    skipped: bool = False


def sentence_case_cue_text(text: str) -> str:
    """Normalize ALL-CAPS English ASR style into sentence case for editors."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    letters = [ch for ch in cleaned if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        lowered = cleaned.lower()
        return lowered[:1].upper() + lowered[1:]
    return cleaned


def normalize_srt_casing(srt_text: str) -> str:
    cues = [
        Cue(
            index=cue.index,
            start=cue.start,
            end=cue.end,
            text=sentence_case_cue_text(cue.text),
        )
        for cue in parse_srt(srt_text)
    ]
    return write_srt(cues)


def ai_prefill_enabled() -> bool:
    return os.getenv("SMARTCAT_AI_PREFILL", "").strip().lower() in {"1", "true", "yes"}


def resolve_context_from_editor_link(
    editor_link: str,
    *,
    title: str | None = None,
    target_language_id: str | int = 1026,
) -> SmartcatDocumentContext:
    from catalog_parser.smartcat import parse_smartcat_resource_link

    parsed = parse_smartcat_resource_link(editor_link)
    if parsed is None or not parsed.document_id:
        raise SmartcatError(f"Could not parse Smartcat editor link: {editor_link!r}")
    language_id = (
        str(parsed.target_language_id)
        if parsed.target_language_id is not None
        else str(target_language_id)
    )
    return SmartcatDocumentContext(
        project_id=parsed.project_id or "",
        document_id=parsed.document_id,
        document_name=title or parsed.document_id,
        search=parsed.search or title,
        source_language_id="9",
        target_language_id=language_id,
    )


def prefill_document_from_english(
    client: SmartcatWebRequestClient,
    context: SmartcatDocumentContext,
    *,
    index_path: Path = DEFAULT_INDEX_PATH,
    pairs_path: Path = DEFAULT_PAIRS_PATH,
    holdout_path: Path = DEFAULT_HOLDOUT_PATH,
    top_k: int = DEFAULT_TOP_K,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> PrefillResult:
    try:
        from catalog_parser.smartcat import (
            bulgarian_target_is_fully_done,
            get_language_target,
        )

        status, payload = client.web_request("GET", f"/api/Documents/{context.document_id}")
        if status >= 400:
            raise SmartcatError(
                f"Could not load Smartcat document {context.document_id!r} "
                f"(HTTP {status})"
            )
        document = json.loads(payload.decode("utf-8"))
        target = get_language_target(document, int(context.target_language_id))
        if target is not None and bulgarian_target_is_fully_done(target):
            return PrefillResult(ok=True, skipped=True)

        # Prefer real segment text over workflow progress (progress can stay 0
        # even when targets already contain human Bulgarian).
        from catalog_parser.smartcat_write import list_document_segments

        existing = list_document_segments(
            client,
            context.document_id,
            int(context.target_language_id),
        )
        filled = 0
        for segment in existing:
            for item in segment.get("targets") or []:
                if not isinstance(item, dict):
                    continue
                if int(item.get("languageId") or 0) != int(context.target_language_id):
                    continue
                if str(item.get("text") or "").strip():
                    filled += 1
        if existing and filled / max(len(existing), 1) >= 0.5:
            return PrefillResult(ok=True, skipped=True)

        source_srt = export_document_srt_via_web_api(
            client,
            context.document_id,
            int(context.target_language_id),
            export_type=WEB_EXPORT_TYPE_SOURCE,
            segment_export_mode=WEB_SEGMENT_EXPORT_MODE_SOURCE,
        )
        source_cues = parse_srt(source_srt)
        if not source_cues:
            return PrefillResult(ok=False, error="English SRT export was empty")

        index = load_or_build_index(
            index_path=index_path,
            pairs_path=pairs_path,
            holdout_path=holdout_path,
        )
        config = chat_config_from_env()
        ai_srt = translate_srt_text(
            source_srt,
            index,
            config,
            top_k=top_k,
            batch_size=batch_size,
        )
        ai_srt = normalize_srt_casing(ai_srt)
        written = SmartcatWebSrtImporter(client).import_target_srt(context, ai_srt)
        return PrefillResult(
            ok=True,
            source_cues=len(source_cues),
            written_segments=written,
        )
    except Exception as exc:  # noqa: BLE001 - ingest must continue on failure
        return PrefillResult(ok=False, error=str(exc))


def prefill_record_if_needed(
    record: dict[str, Any],
    client: SmartcatWebRequestClient,
    *,
    project_root: Path,
    language_id: int = 1026,
) -> PrefillResult:
    if not ai_prefill_enabled():
        return PrefillResult(ok=True, skipped=True)

    editor_link = record.get("pkgBgSrtLk")
    if not isinstance(editor_link, str) or not editor_link.strip():
        return PrefillResult(ok=False, error="Missing Smartcat editor link for AI prefill")

    title = record.get("ctTitle") if isinstance(record.get("ctTitle"), str) else None
    context = resolve_context_from_editor_link(
        editor_link,
        title=title,
        target_language_id=language_id,
    )
    return prefill_document_from_english(
        client,
        context,
        index_path=project_root / DEFAULT_INDEX_PATH,
        pairs_path=project_root / DEFAULT_PAIRS_PATH,
        holdout_path=project_root / DEFAULT_HOLDOUT_PATH,
    )
