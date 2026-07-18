"""Write Bulgarian target segment text into Smartcat via cookie web APIs."""
from __future__ import annotations

import json
from typing import Any

from catalog_parser.smartcat import SmartcatError
from catalog_parser.smartcat_export import SmartcatDocumentContext, SmartcatWebRequestClient
from catalog_parser.translation.srt import Cue, parse_srt

# Editor manager mode can update targets without being assigned as translator.
WEB_EDITOR_MODE_MANAGER = "manager"
WEB_EDITOR_STAGE_NUMBER = 0
WEB_SAVE_TYPE_AUTO_SAVED = 0
WEB_SEGMENTS_PAGE_LIMIT = 128
DEFAULT_CUE_JOIN_SEPARATOR = "\n"


def list_document_segments(
    client: SmartcatWebRequestClient,
    document_id: str,
    language_id: int,
    *,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
    page_limit: int = WEB_SEGMENTS_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    start = 0
    total: int | None = None
    while total is None or start < total:
        status, payload = client.web_request(
            "GET",
            "/api/Segments",
            params={
                "documentId": document_id,
                "languageId": language_id,
                "start": start,
                "limit": page_limit,
                "mode": mode,
                "stageNumber": stage_number,
            },
        )
        if status >= 400:
            detail = payload.decode("utf-8", errors="replace")[:500]
            raise SmartcatError(
                f"Smartcat Segments list failed for {document_id!r} "
                f"(HTTP {status}): {detail}"
            )
        data = json.loads(payload.decode("utf-8")) if payload else {}
        if not isinstance(data, dict):
            raise SmartcatError(f"Unexpected Segments payload: {data!r}")
        batch = data.get("items")
        if not isinstance(batch, list):
            raise SmartcatError(f"Segments payload missing items: {data!r}")
        items.extend(item for item in batch if isinstance(item, dict))
        reported_total = data.get("total")
        total = int(reported_total) if isinstance(reported_total, int) else start + len(batch)
        if not batch:
            break
        start += len(batch)
    return items


def segment_source_text(segment: dict[str, Any]) -> str:
    source = segment.get("source")
    if not isinstance(source, dict):
        return ""
    text = source.get("text")
    return text if isinstance(text, str) else ""


def segment_subtitle_ids(segment: dict[str, Any]) -> list[int]:
    raw = segment.get("subtitleId")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def segment_source_tags(segment: dict[str, Any]) -> list[dict[str, Any]]:
    source = segment.get("source")
    if not isinstance(source, dict):
        return []
    tags = source.get("tags")
    if not isinstance(tags, list):
        return []
    return [tag for tag in tags if isinstance(tag, dict)]


def _is_subtitle_tag(tag: dict[str, Any]) -> bool:
    return bool(tag.get("isSubtitleTag") or int(tag.get("tagType") or 0) == 2)


def _en_break_is_before_quote(en: str, pos: int) -> bool:
    after = (en[pos:] if pos < len(en) else "").lstrip()
    return after[:1] in {'"', "“", "„", "”"}


def _snap_break_to_space(text: str, pos: int) -> int:
    if not text:
        return 0
    pos = max(0, min(pos, len(text)))
    if pos < len(text) and text[pos] == " ":
        return pos
    for delta in range(1, 16):
        left = pos - delta
        right = pos + delta
        if left >= 0 and text[left] == " ":
            return left
        if right < len(text) and text[right] == " ":
            return right
    return pos


def _bg_break_at_opening_quote(bg: str, approx: int) -> int | None:
    """Prefer breaking at „ so a preceding ':' stays on the previous cue."""
    if not bg:
        return None
    positions = [index for index, char in enumerate(bg) if char == "„"]
    if not positions:
        for marker in (": ", ":"):
            rel = bg.find(marker)
            if rel >= 0:
                return rel + len(marker)
        return None

    def score(pos: int) -> tuple[int, int]:
        # Prefer „ that follows a colon (direct speech after КАЗА:/ПОПИТА:),
        # then nearest to the proportional estimate.
        before = bg[max(0, pos - 3) : pos]
        colon_bonus = 0
        if before.endswith(": ") or before.endswith(":"):
            colon_bonus = -1000
        return (colon_bonus + abs(pos - approx), pos)

    return min(positions, key=score)


def _avoid_colon_break(bg: str, pos: int) -> int:
    """Never leave a subtitle break sitting on ':' (causes ': „…' cue starts)."""
    if not bg:
        return 0
    pos = max(0, min(pos, len(bg)))
    while pos < len(bg) and bg[pos] == ":":
        pos += 1
    while pos < len(bg) and bg[pos] == " ":
        # Prefer landing on „ immediately after ": "
        if pos + 1 <= len(bg) and pos < len(bg) and bg[pos:].lstrip()[:1] == "„":
            return pos + bg[pos:].find("„")
        break
    if pos < len(bg) and bg[pos] == "„":
        return pos
    return pos


def map_subtitle_tags_to_translation(
    source_text: str,
    target_text: str,
    source_tags: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Remap English subtitle-tag positions onto Bulgarian target text.

    Smartcat's auto-remap often parks a break on BG ':' when EN broke before
    a quote. We place those breaks on the opening „ instead so the colon stays
    with the verb cue (…КАЗА: / …ПОПИТА:).
    """
    en = source_text or ""
    bg = target_text or ""
    subtitle_tags = [tag for tag in source_tags if _is_subtitle_tag(tag)]
    if not subtitle_tags or not bg:
        return []

    rebuilt: list[dict[str, Any]] = []
    last_pos = -1
    ordered = sorted(subtitle_tags, key=lambda tag: int(tag.get("position") or 0))
    for tag in ordered:
        en_pos = max(0, min(int(tag.get("position") or 0), len(en)))
        approx = int(round((en_pos / len(en)) * len(bg))) if en else 0
        if _en_break_is_before_quote(en, en_pos):
            snapped = _bg_break_at_opening_quote(bg, approx)
            if snapped is None:
                snapped = _snap_break_to_space(bg, approx)
        else:
            snapped = _snap_break_to_space(bg, approx)
        snapped = _avoid_colon_break(bg, snapped)
        snapped = max(last_pos + 1, min(snapped, len(bg)))
        last_pos = snapped
        new_tag = dict(tag)
        new_tag["position"] = snapped
        rebuilt.append(new_tag)
    return rebuilt


def cue_texts_for_subtitle_ids(
    cues_by_index: dict[int, Cue],
    subtitle_ids: list[int],
) -> list[str]:
    parts: list[str] = []
    for subtitle_id in subtitle_ids:
        cue = cues_by_index.get(subtitle_id)
        if cue is None:
            continue
        text = cue.text.strip()
        if text:
            parts.append(text)
    return parts


def rebuild_subtitle_tags_for_parts(
    source_tags: list[dict[str, Any]],
    parts: list[str],
    *,
    separator: str = DEFAULT_CUE_JOIN_SEPARATOR,
) -> list[dict[str, Any]]:
    """Place subtitle tags at join boundaries between translated cue parts."""
    if len(parts) < 2:
        return []
    subtitle_tags = [
        tag
        for tag in source_tags
        if tag.get("isSubtitleTag") or int(tag.get("tagType") or 0) == 2
    ]
    if not subtitle_tags:
        return []
    rebuilt: list[dict[str, Any]] = []
    for index, tag in enumerate(subtitle_tags):
        if index >= len(parts) - 1:
            break
        position = len(separator.join(parts[: index + 1]))
        new_tag = dict(tag)
        new_tag["position"] = position
        rebuilt.append(new_tag)
    return rebuilt


def target_text_and_tags_for_segment(
    segment: dict[str, Any],
    cues: list[Cue],
    *,
    segment_index: int,
    separator: str = DEFAULT_CUE_JOIN_SEPARATOR,
) -> tuple[str, list[dict[str, Any]]] | None:
    """
    Map translated SRT cues onto one Smartcat segment.

    Prefer ``subtitleId`` (merged subtitle units). Fall back to positional
    ``cues[segment_index]`` when the document is 1:1.
    """
    cues_by_index = {cue.index: cue for cue in cues}
    subtitle_ids = segment_subtitle_ids(segment)
    if subtitle_ids:
        parts = cue_texts_for_subtitle_ids(cues_by_index, subtitle_ids)
        if not parts:
            return None
        text = separator.join(parts)
        tags = rebuild_subtitle_tags_for_parts(
            segment_source_tags(segment),
            parts,
            separator=separator,
        )
        return text, tags
    if segment_index >= len(cues):
        return None
    text = cues[segment_index].text.strip()
    if not text:
        return None
    return text, []


def update_segment_target_text(
    client: SmartcatWebRequestClient,
    *,
    document_id: str,
    segment_id: int,
    language_id: int,
    text: str,
    tags: list[dict[str, Any]] | None = None,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
    save_type: int = WEB_SAVE_TYPE_AUTO_SAVED,
    auto_populate_target_tags: bool = True,
) -> None:
    status, payload = client.web_request(
        "PUT",
        f"/api/v2/Segments/{segment_id}/SegmentTargets/{language_id}",
        params={
            "documentId": document_id,
            "saveType": save_type,
            "mode": mode,
            "stageNumber": stage_number,
            "autoPopulateTargetTags": (
                "true" if auto_populate_target_tags else "false"
            ),
            "isUnfocused": "false",
        },
        json_body={
            "text": text,
            "tags": tags if tags is not None else [],
            "tmTranslation": None,
        },
    )
    if status >= 400:
        detail = payload.decode("utf-8", errors="replace")[:500]
        raise SmartcatError(
            f"Smartcat segment update failed for segment={segment_id} "
            f"lang={language_id} (HTTP {status}): {detail}"
        )


def write_translated_texts_to_segments(
    client: SmartcatWebRequestClient,
    context: SmartcatDocumentContext,
    segments: list[dict[str, Any]],
    translations: list[str],
    *,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
) -> int:
    """Write one translation per Smartcat segment (aligned to segment source text)."""
    if len(translations) != len(segments):
        raise SmartcatError(
            f"Translation count {len(translations)} does not match "
            f"segment count {len(segments)}"
        )
    language_id = int(context.target_language_id)
    updated = 0
    for segment, text in zip(segments, translations):
        segment_id = segment.get("id")
        if not isinstance(segment_id, int):
            continue
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        tags = map_subtitle_tags_to_translation(
            segment_source_text(segment),
            cleaned,
            segment_source_tags(segment),
        )
        update_segment_target_text(
            client,
            document_id=context.document_id,
            segment_id=segment_id,
            language_id=language_id,
            text=cleaned,
            tags=tags,
            mode=mode,
            stage_number=stage_number,
            # We already mapped positions onto BG; don't let Smartcat re-park
            # breaks on ':' again.
            auto_populate_target_tags=False,
        )
        updated += 1
    return updated


def write_target_texts_from_cues(
    client: SmartcatWebRequestClient,
    context: SmartcatDocumentContext,
    cues: list[Cue],
    *,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
    separator: str = DEFAULT_CUE_JOIN_SEPARATOR,
) -> int:
    """Write cue texts into Smartcat BG targets. Returns updated segment count."""
    language_id = int(context.target_language_id)
    segments = list_document_segments(
        client,
        context.document_id,
        language_id,
        mode=mode,
        stage_number=stage_number,
    )
    if not segments:
        raise SmartcatError(f"No Smartcat segments for document {context.document_id!r}")
    if not cues:
        raise SmartcatError("No cues to write into Smartcat")

    updated = 0
    for index, segment in enumerate(segments):
        segment_id = segment.get("id")
        if not isinstance(segment_id, int):
            continue
        mapped = target_text_and_tags_for_segment(
            segment,
            cues,
            segment_index=index,
            separator=separator,
        )
        if mapped is None:
            continue
        text, tags = mapped
        update_segment_target_text(
            client,
            document_id=context.document_id,
            segment_id=segment_id,
            language_id=language_id,
            text=text,
            tags=tags,
            mode=mode,
            stage_number=stage_number,
        )
        updated += 1
    return updated


def write_target_srt(
    client: SmartcatWebRequestClient,
    context: SmartcatDocumentContext,
    target_srt: str,
) -> int:
    return write_target_texts_from_cues(client, context, parse_srt(target_srt))


class SmartcatWebSrtImporter:
    def __init__(self, client: SmartcatWebRequestClient) -> None:
        self._client = client

    def import_target_srt(self, context: SmartcatDocumentContext, target_srt: str) -> int:
        return write_target_srt(self._client, context, target_srt)
