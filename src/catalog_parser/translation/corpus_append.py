"""Append one Airtable record's EN↔BG pairs into the local translation corpus."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TextIO

from catalog_parser.airtable import AirtableClient
from catalog_parser.smartcat import (
    DEFAULT_TARGET_LANGUAGE,
    SmartcatError,
    parse_pkg_sm_link,
    parse_smartcat_resource_link,
    resolve_language_id,
)
from catalog_parser.smartcat_export import (
    SmartcatApiSrtExporter,
    SmartcatDocumentContext,
    SmartcatWebSrtExporter,
    build_api_client_from_env,
    build_cookie_client_from_env,
    build_web_client_from_env,
    resolve_context_from_smartcat_link,
    resolve_source_language_id,
)
from catalog_parser.smartcat_web import SmartcatWebSession
from catalog_parser.translation.corpus import (
    CorpusCandidate,
    default_current_year,
    is_parseable_smartcat_link,
    records_to_candidates,
)
from catalog_parser.translation.index import (
    DEFAULT_HOLDOUT_PATH,
    DEFAULT_METADATA_PAIRS_PATH,
    DEFAULT_PAIRS_PATH,
    load_holdout_titles,
)
from catalog_parser.translation.metadata_corpus import (
    DriveFieldCache,
    build_pairs_for_candidate,
    metadata_candidate_from_record,
)
from catalog_parser.translation.quality import (
    DEFAULT_MAX_IDENTICAL_RATE,
    DEFAULT_MIN_CYRILLIC_RATE,
    filter_aligned_for_bulgarian,
    passes_bilingual_gates,
)
from catalog_parser.translation.srt import align_cues, parse_srt


@dataclass
class CorpusAppendResult:
    metadata_pairs: int = 0
    subtitle_cues: int = 0
    skipped_metadata: str | None = None
    skipped_subtitles: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts: list[str] = []
        if self.metadata_pairs:
            parts.append(f"{self.metadata_pairs} metadata pair(s)")
        elif self.skipped_metadata:
            parts.append(f"metadata skipped ({self.skipped_metadata})")
        if self.subtitle_cues:
            parts.append(f"{self.subtitle_cues} subtitle cue(s)")
        elif self.skipped_subtitles:
            parts.append(f"subtitles skipped ({self.skipped_subtitles})")
        if self.notes:
            parts.extend(self.notes)
        return "; ".join(parts) if parts else "nothing to add"


def load_jsonl_record_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        record_id = payload.get("record_id")
        if isinstance(record_id, str) and record_id.strip():
            ids.add(record_id.strip())
    return ids


def load_jsonl_video_titles(path: Path) -> set[str]:
    if not path.exists():
        return set()
    titles: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        title = payload.get("video_title")
        if isinstance(title, str) and title.strip():
            titles.add(title.strip())
    return titles


def write_aligned_pairs(
    candidate: CorpusCandidate,
    context: SmartcatDocumentContext,
    source_srt: str,
    target_srt: str,
    *,
    output_handle: TextIO,
    min_cyrillic_rate: float = DEFAULT_MIN_CYRILLIC_RATE,
    max_identical_rate: float = DEFAULT_MAX_IDENTICAL_RATE,
) -> tuple[int, list[str]]:
    ok, gate_reason = passes_bilingual_gates(
        source_srt,
        target_srt,
        min_cyrillic_rate=min_cyrillic_rate,
        max_identical_rate=max_identical_rate,
    )
    if not ok:
        raise SmartcatError(
            f"Quality gate failed for {candidate.title!r}: {gate_reason}"
        )

    source_cues = parse_srt(source_srt)
    target_cues = parse_srt(target_srt)
    aligned, issues = align_cues(source_cues, target_cues)
    kept = filter_aligned_for_bulgarian(aligned)
    dropped = len(aligned) - len(kept)
    if dropped:
        issues.append(f"dropped {dropped} non-Bulgarian/identical cue(s)")
    if not kept:
        raise SmartcatError(
            f"No usable bilingual cues for {candidate.title!r} "
            f"(source={len(source_cues)} target={len(target_cues)} "
            f"aligned={len(aligned)})"
        )

    for pair in kept:
        payload = {
            "video_title": candidate.title,
            "record_id": candidate.record_id,
            "status": candidate.status,
            "record_type": candidate.record_type,
            "translated_title": candidate.translated_title,
            "source": candidate.source,
            "base_id": candidate.base_id,
            "table_name": candidate.table_name,
            "document_name": context.document_name,
            "project_id": context.project_id,
            "document_id": context.document_id,
            "cue_index": pair.cue_index,
            "start": pair.start,
            "end": pair.end,
            "en": pair.source_text,
            "bg": pair.target_text,
        }
        output_handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return len(kept), issues


def resolve_web_document_context(
    web_client: Any,
    candidate: CorpusCandidate,
    *,
    target_language: str,
) -> SmartcatDocumentContext:
    parsed_project = parse_pkg_sm_link(candidate.smartcat_link)
    parsed_editor = parse_smartcat_resource_link(candidate.smartcat_link)

    target_language_id = str(resolve_language_id(target_language))
    if parsed_editor and parsed_editor.target_language_id is not None:
        target_language_id = str(parsed_editor.target_language_id)

    if parsed_editor is not None and parsed_editor.document_id and parsed_project is None:
        if not parsed_editor.project_id:
            return SmartcatDocumentContext(
                project_id="",
                document_id=parsed_editor.document_id,
                document_name=candidate.title,
                search=parsed_editor.search,
                source_language_id="9",
                target_language_id=target_language_id,
            )

    if parsed_project is not None:
        project_id = parsed_project.project_id
        search = parsed_project.search
        document_id = parsed_editor.document_id if parsed_editor else None
    elif parsed_editor is not None and parsed_editor.project_id:
        project_id = parsed_editor.project_id
        search = parsed_editor.search or candidate.title
        document_id = parsed_editor.document_id
    else:
        raise SmartcatError(f"Could not parse Smartcat link: {candidate.smartcat_link!r}")

    if isinstance(document_id, str) and document_id:
        return SmartcatDocumentContext(
            project_id=project_id,
            document_id=document_id,
            document_name=candidate.title,
            search=search,
            source_language_id="9",
            target_language_id=target_language_id,
        )

    find_document = getattr(web_client, "find_document", None)
    if callable(find_document):
        document = find_document(
            project_id,
            search=search or candidate.title,
            title=candidate.title,
        )
    else:
        document = web_client._find_document(
            project_id,
            search=search or candidate.title,
            title=candidate.title,
        )
    resolved_document_id = document.get("id")
    if not isinstance(resolved_document_id, str) or not resolved_document_id:
        raise SmartcatError(f"Matched Smartcat document is missing an id: {document!r}")

    return SmartcatDocumentContext(
        project_id=project_id,
        document_id=resolved_document_id,
        document_name=str(
            document.get("name") or document.get("fileName") or candidate.title
        ),
        search=search,
        source_language_id=resolve_source_language_id(document, {}),
        target_language_id=target_language_id,
    )


def append_metadata_pairs_for_record(
    record: dict[str, Any],
    *,
    airtable: AirtableClient,
    project_root: Path,
    drive_service: Any | None = None,
    docs_service: Any | None = None,
    metadata_path: Path | None = None,
    current_year: str | None = None,
) -> tuple[int, str | None, list[str]]:
    """Append title/description pairs when BG (+ EN) text is present.

    Returns ``(pair_count, skip_reason, notes)``.
    """
    output = (
        metadata_path
        if metadata_path is not None
        else _resolve_path(project_root, DEFAULT_METADATA_PAIRS_PATH)
    )
    year = current_year or os.getenv("CORPUS_CURRENT_YEAR", "").strip() or default_current_year()
    candidate = metadata_candidate_from_record(
        record,
        source=year,
        base_id=airtable.base_id,
        table_name=airtable.table_name,
    )
    if candidate is None:
        return 0, "no title on record", []
    if not candidate.bg_title and not candidate.bg_description:
        return 0, "no BG title or description", []
    if candidate.record_id in load_jsonl_record_ids(output):
        return 0, "already in metadata corpus", []

    drive_cache = None
    if drive_service is not None:
        drive_cache = DriveFieldCache(drive_service, docs_service)

    pairs, notes = build_pairs_for_candidate(candidate, drive_cache)
    if not pairs:
        return 0, "; ".join(notes) if notes else "no pairs", notes

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        for pair in pairs:
            handle.write(json.dumps(asdict(pair), ensure_ascii=False) + "\n")
    return len(pairs), None, notes


def append_subtitle_pairs_for_record(
    record: dict[str, Any],
    *,
    airtable: AirtableClient,
    project_root: Path,
    subtitle_path: Path | None = None,
    holdout_path: Path | None = None,
    current_year: str | None = None,
    target_language: str | None = None,
) -> tuple[int, str | None, list[str]]:
    """Export bilingual Smartcat SRTs and append cue pairs when possible.

    Returns ``(cue_count, skip_reason, notes)``.
    """
    output = (
        subtitle_path
        if subtitle_path is not None
        else _resolve_path(project_root, DEFAULT_PAIRS_PATH)
    )
    holdout = (
        holdout_path
        if holdout_path is not None
        else _resolve_path(project_root, DEFAULT_HOLDOUT_PATH)
    )
    year = current_year or os.getenv("CORPUS_CURRENT_YEAR", "").strip() or default_current_year()
    language = (
        target_language
        or os.getenv("SMARTCAT_TARGET_LANGUAGE", "").strip()
        or DEFAULT_TARGET_LANGUAGE
    )

    candidates = records_to_candidates(
        [record],
        source=year,
        base_id=airtable.base_id,
        table_name=airtable.table_name,
    )
    if not candidates:
        fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
        link = fields.get("Translation resources")
        if not isinstance(link, str) or not is_parseable_smartcat_link(link):
            return 0, "no Smartcat link", []
        return 0, "not eligible for subtitle corpus", []

    candidate = candidates[0]
    holdout_titles = load_holdout_titles(holdout) if holdout.exists() else set()
    if candidate.title in holdout_titles:
        return 0, "holdout title", []
    if candidate.record_id in load_jsonl_record_ids(output):
        return 0, "already in subtitle corpus", []
    if candidate.title in load_jsonl_video_titles(output):
        return 0, "already in subtitle corpus", []

    cue_count, notes = _export_and_write_subtitle_pairs(
        candidate,
        project_root=project_root,
        output_path=output,
        target_language=language,
    )
    return cue_count, None, notes


def append_record_to_corpus(
    record: dict[str, Any],
    *,
    airtable: AirtableClient,
    project_root: Path,
    drive_service: Any | None = None,
    docs_service: Any | None = None,
) -> CorpusAppendResult:
    """Best-effort append of metadata and subtitle pairs for one record."""
    result = CorpusAppendResult()

    try:
        count, skipped, notes = append_metadata_pairs_for_record(
            record,
            airtable=airtable,
            project_root=project_root,
            drive_service=drive_service,
            docs_service=docs_service,
        )
        result.metadata_pairs = count
        result.skipped_metadata = skipped
        result.notes.extend(notes)
    except Exception as exc:  # noqa: BLE001 — never block workflow on corpus I/O
        result.skipped_metadata = str(exc)

    try:
        count, skipped, notes = append_subtitle_pairs_for_record(
            record,
            airtable=airtable,
            project_root=project_root,
        )
        result.subtitle_cues = count
        result.skipped_subtitles = skipped
        result.notes.extend(notes)
    except Exception as exc:  # noqa: BLE001 — never block workflow on corpus I/O
        result.skipped_subtitles = str(exc)

    return result


def _resolve_path(project_root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    return project_root / path


def _export_and_write_subtitle_pairs(
    candidate: CorpusCandidate,
    *,
    project_root: Path,
    output_path: Path,
    target_language: str,
) -> tuple[int, list[str]]:
    account_id = os.getenv("SMARTCAT_ACCOUNT_ID", "").strip()
    api_key = os.getenv("SMARTCAT_API_KEY", "").strip()
    use_api = bool(account_id and api_key)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if use_api:
        api_client = build_api_client_from_env()
        exporter = SmartcatApiSrtExporter(api_client)
        context = resolve_context_from_smartcat_link(
            api_client,
            candidate.smartcat_link,
            title=candidate.title,
            target_language=target_language,
        )
        source_srt, target_srt = exporter.export_bilingual_pair(context)
        with output_path.open("a", encoding="utf-8") as handle:
            return write_aligned_pairs(
                candidate,
                context,
                source_srt,
                target_srt,
                output_handle=handle,
            )

    # Prefer cookie client (no Playwright) when a storage-state file exists.
    try:
        cookie_client = build_cookie_client_from_env(project_root=project_root)
        exporter = SmartcatWebSrtExporter(cookie_client)
        context = resolve_web_document_context(
            cookie_client,
            candidate,
            target_language=target_language,
        )
        source_srt, target_srt = exporter.export_bilingual_pair(context)
        with output_path.open("a", encoding="utf-8") as handle:
            return write_aligned_pairs(
                candidate,
                context,
                source_srt,
                target_srt,
                output_handle=handle,
            )
    except Exception:
        pass

    web_client = build_web_client_from_env(project_root=project_root)
    exporter = SmartcatWebSrtExporter(web_client)
    with SmartcatWebSession(web_client):
        context = resolve_web_document_context(
            web_client,
            candidate,
            target_language=target_language,
        )
        source_srt, target_srt = exporter.export_bilingual_pair(context)
        with output_path.open("a", encoding="utf-8") as handle:
            return write_aligned_pairs(
                candidate,
                context,
                source_srt,
                target_srt,
                output_handle=handle,
            )
