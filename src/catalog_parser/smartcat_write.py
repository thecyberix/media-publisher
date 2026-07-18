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


def update_segment_target_text(
    client: SmartcatWebRequestClient,
    *,
    document_id: str,
    segment_id: int,
    language_id: int,
    text: str,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
    save_type: int = WEB_SAVE_TYPE_AUTO_SAVED,
) -> None:
    status, payload = client.web_request(
        "PUT",
        f"/api/v2/Segments/{segment_id}/SegmentTargets/{language_id}",
        params={
            "documentId": document_id,
            "saveType": save_type,
            "mode": mode,
            "stageNumber": stage_number,
            "autoPopulateTargetTags": "true",
            "isUnfocused": "false",
        },
        json_body={"text": text, "tags": [], "tmTranslation": None},
    )
    if status >= 400:
        detail = payload.decode("utf-8", errors="replace")[:500]
        raise SmartcatError(
            f"Smartcat segment update failed for segment={segment_id} "
            f"lang={language_id} (HTTP {status}): {detail}"
        )


def write_target_texts_from_cues(
    client: SmartcatWebRequestClient,
    context: SmartcatDocumentContext,
    cues: list[Cue],
    *,
    mode: str = WEB_EDITOR_MODE_MANAGER,
    stage_number: int = WEB_EDITOR_STAGE_NUMBER,
) -> int:
    """Write cue texts into Smartcat BG targets by segment order. Returns updated count."""
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
        if index >= len(cues):
            break
        segment_id = segment.get("id")
        if not isinstance(segment_id, int):
            continue
        text = cues[index].text.strip()
        if not text:
            continue
        update_segment_target_text(
            client,
            document_id=context.document_id,
            segment_id=segment_id,
            language_id=language_id,
            text=text,
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
