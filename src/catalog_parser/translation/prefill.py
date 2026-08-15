"""Ingest-time AI prefill of Bulgarian Smartcat targets from English source."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from catalog_parser.smartcat import SmartcatError
from catalog_parser.smartcat_export import (
    SmartcatDocumentContext,
    SmartcatWebRequestClient,
)
from catalog_parser.smartcat_write import (
    list_document_segments,
    segment_source_text,
    write_translated_texts_to_segments,
)
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
    requires_all_caps,
    translate_cue_texts,
    translation_provider_disabled,
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
    """Normalize ALL-CAPS English ASR style into sentence case for editors.

    Preserves existing line breaks so subtitle layout stays intact.
    """
    if not text:
        return ""

    def _case_line(line: str) -> str:
        cleaned = " ".join(line.split())
        if not cleaned:
            return ""
        letters = [ch for ch in cleaned if ch.isalpha()]
        if letters and all(ch.isupper() for ch in letters):
            lowered = cleaned.lower()
            return lowered[:1].upper() + lowered[1:]
        return cleaned

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(_case_line(line) for line in normalized.split("\n"))


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


def resolve_record_type(record: dict[str, Any]) -> str | None:
    for key in ("Type", "ctType", "type", "record_type"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def ai_prefill_enabled() -> bool:
    if translation_provider_disabled():
        return False
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
    record_type: str | None = None,
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
        # even when targets already contain human Bulgarian). Only skip when a
        # majority of targets are confirmed — unconfirmed AI drafts may be
        # rewritten (e.g. after fixing cue↔segment alignment).
        existing = list_document_segments(
            client,
            context.document_id,
            int(context.target_language_id),
        )
        confirmed = 0
        for segment in existing:
            for item in segment.get("targets") or []:
                if not isinstance(item, dict):
                    continue
                if int(item.get("languageId") or 0) != int(context.target_language_id):
                    continue
                if item.get("isConfirmed") and str(item.get("text") or "").strip():
                    confirmed += 1
                    break
        if existing and confirmed / max(len(existing), 1) >= 0.5:
            return PrefillResult(ok=True, skipped=True)

        # Translate each Smartcat segment's English source.text. Do not join SRT
        # cues by subtitleId: adjacent segments often share boundary cues, which
        # duplicates those cue translations in the editor.
        writable: list[dict[str, Any]] = []
        source_texts: list[str] = []
        for segment in existing:
            raw = segment_source_text(segment)
            if not raw.strip():
                continue
            writable.append(segment)
            source_texts.append(sentence_case_cue_text(raw))
        if not source_texts:
            return PrefillResult(ok=False, error="No English segment source text found")

        index = load_or_build_index(
            index_path=index_path,
            pairs_path=pairs_path,
            holdout_path=holdout_path,
        )
        config = chat_config_from_env()
        translations = translate_cue_texts(
            source_texts,
            index,
            config,
            top_k=top_k,
            batch_size=batch_size,
            record_type=record_type,
        )
        if not requires_all_caps(record_type):
            translations = [sentence_case_cue_text(text) for text in translations]
        written = write_translated_texts_to_segments(
            client,
            context,
            writable,
            translations,
        )
        return PrefillResult(
            ok=True,
            source_cues=len(source_texts),
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
        record_type=resolve_record_type(record),
    )
