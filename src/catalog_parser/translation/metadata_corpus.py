"""Airtable + Drive metadata (title/description) pairs for translation RAG."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_DESCRIPTION,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    FIELD_VIDEO_NAME_TRANSLATED,
)
from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    DEFAULT_YT_TITLE_FIELD,
    DriveDocsError,
    extract_drive_folder_id,
    read_drive_fields_from_folder,
)
from catalog_parser.translation.corpus import (
    DEFAULT_TITLE_FIELDS,
    CorpusCandidate,
    build_corpus_selection,
    resolve_record_title,
)


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class MetadataCandidate:
    record_id: str
    title: str
    status: str
    record_type: str
    source: str
    base_id: str
    table_name: str
    video_folder: str | None
    bg_title: str | None
    bg_description: str | None
    en_title_airtable: str | None
    en_description_airtable: str | None


@dataclass(frozen=True)
class MetadataPair:
    kind: str  # "title" | "description"
    en: str
    bg: str
    video_title: str
    record_id: str
    record_type: str
    status: str
    source: str
    base_id: str
    table_name: str
    en_origin: str  # "airtable" | "drive"


def metadata_candidate_from_record(
    record: dict[str, Any],
    *,
    source: str,
    base_id: str,
    table_name: str,
    title_fields: tuple[str, ...] = DEFAULT_TITLE_FIELDS,
) -> MetadataCandidate | None:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    title = resolve_record_title(fields, title_fields=title_fields)
    if not title:
        return None
    record_id = str(record.get("id") or "").strip()
    if not record_id:
        return None
    return MetadataCandidate(
        record_id=record_id,
        title=title,
        status=_field_text(fields.get(FIELD_STATUS)) or "",
        record_type=_field_text(fields.get(FIELD_TYPE)) or "",
        source=source,
        base_id=base_id,
        table_name=table_name,
        video_folder=_field_text(fields.get(FIELD_VIDEO_FOLDER)),
        bg_title=_field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)),
        bg_description=_field_text(fields.get(FIELD_VIDEO_DESCRIPTION_TRANSLATED)),
        en_title_airtable=_field_text(fields.get(FIELD_ORIGINAL_VIDEO_NAME)),
        en_description_airtable=_field_text(fields.get(FIELD_ORIGINAL_VIDEO_DESCRIPTION)),
    )


def corpus_candidate_to_lookup_key(candidate: CorpusCandidate) -> tuple[str, str]:
    return candidate.base_id, candidate.record_id


def load_metadata_candidates_for_corpus(
    airtable: AirtableClient,
    *,
    current_year: str,
    holdout_count: int = 30,
    holdout_seed: str = "media-publisher-corpus-holdout",
    include_archives: bool = True,
    include_current: bool = True,
) -> list[MetadataCandidate]:
    """Reuse subtitle corpus selection, then reload Airtable rows for metadata fields."""
    selection = build_corpus_selection(
        airtable,
        current_year=current_year,
        holdout_count=holdout_count,
        holdout_seed=holdout_seed,
        include_archives=include_archives,
        include_current=include_current,
    )
    wanted = {
        corpus_candidate_to_lookup_key(candidate)
        for candidate in selection.export_candidates
    }

    by_key: dict[tuple[str, str], MetadataCandidate] = {}

    def ingest(
        records: list[dict[str, Any]],
        *,
        source: str,
        base_id: str,
        table_name: str,
        title_fields: tuple[str, ...],
    ) -> None:
        for record in records:
            meta = metadata_candidate_from_record(
                record,
                source=source,
                base_id=base_id,
                table_name=table_name,
                title_fields=title_fields,
            )
            if meta is None:
                continue
            key = (meta.base_id, meta.record_id)
            if key in wanted:
                by_key[key] = meta

    from catalog_parser.translation.corpus import (
        archive_year_by_base_id,
        build_corpus_query_filter,
    )
    from catalog_parser.workflow.archive_sources import resolve_archive_sources

    if include_current:
        raw = airtable.list_records(
            filter_formula=build_corpus_query_filter(),
            base_id=airtable.base_id,
            table_name=airtable.table_name,
        )
        ingest(
            raw,
            source=str(current_year),
            base_id=airtable.base_id,
            table_name=airtable.table_name,
            title_fields=DEFAULT_TITLE_FIELDS,
        )

    if include_archives:
        year_by_base = archive_year_by_base_id(airtable)
        for source in resolve_archive_sources(airtable):
            year = year_by_base.get(source.base_id, "archive")
            label = f"{year} archive"
            raw = airtable.list_records(
                filter_formula=build_corpus_query_filter(),
                base_id=source.base_id,
                table_name=source.table_name,
            )
            ingest(
                raw,
                source=label,
                base_id=source.base_id,
                table_name=source.table_name,
                title_fields=source.title_fields,
            )

    # Preserve corpus export order when possible
    ordered: list[MetadataCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in selection.export_candidates:
        key = corpus_candidate_to_lookup_key(candidate)
        meta = by_key.get(key)
        if meta is not None and key not in seen:
            ordered.append(meta)
            seen.add(key)
    return ordered


class DriveFieldCache:
    def __init__(self, drive_service: Any, docs_service: Any | None) -> None:
        self._drive = drive_service
        self._docs = docs_service
        self._cache: dict[str, dict[str, str | None]] = {}
        self._errors: dict[str, str] = {}

    @property
    def cached_folder_count(self) -> int:
        return len(self._cache)

    def fields_for_folder_url(self, folder_url: str) -> dict[str, str | None]:
        folder_id = extract_drive_folder_id(folder_url)
        if not folder_id:
            raise DriveDocsError(f"Could not parse Drive folder id from {folder_url!r}")
        if folder_id in self._cache:
            return self._cache[folder_id]
        if folder_id in self._errors:
            raise DriveDocsError(self._errors[folder_id])
        try:
            # Skip video-size probing (can download full videos); title/desc only.
            fields = read_drive_fields_from_folder(
                self._drive,
                self._docs,
                folder_id,
                resolve_video_size=False,
            )
        except Exception as exc:
            self._errors[folder_id] = str(exc)
            raise
        self._cache[folder_id] = fields
        return fields


def resolve_english_fields(
    candidate: MetadataCandidate,
    drive_cache: DriveFieldCache | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Return (en_title, en_description, title_origin, description_origin).
    Origins are 'airtable', 'drive', or None when missing.
    """
    en_title = candidate.en_title_airtable
    en_desc = candidate.en_description_airtable
    title_origin = "airtable" if en_title else None
    desc_origin = "airtable" if en_desc else None

    need_drive = (
        (candidate.bg_title and not en_title) or (candidate.bg_description and not en_desc)
    )
    if need_drive and candidate.video_folder and drive_cache is not None:
        fields = drive_cache.fields_for_folder_url(candidate.video_folder)
        if not en_title:
            drive_title = _field_text(fields.get(DEFAULT_YT_TITLE_FIELD))
            if drive_title:
                en_title = drive_title
                title_origin = "drive"
        if not en_desc:
            drive_desc = _field_text(fields.get(DEFAULT_YT_DESCRIPTION_FIELD))
            if drive_desc:
                en_desc = drive_desc
                desc_origin = "drive"

    return en_title, en_desc, title_origin, desc_origin


def build_pairs_for_candidate(
    candidate: MetadataCandidate,
    drive_cache: DriveFieldCache | None,
) -> tuple[list[MetadataPair], list[str]]:
    """Build title/description pairs; return pairs and skip/error notes."""
    notes: list[str] = []
    if not candidate.bg_title and not candidate.bg_description:
        notes.append("no BG title or description in Airtable")
        return [], notes

    try:
        en_title, en_desc, title_origin, desc_origin = resolve_english_fields(
            candidate, drive_cache
        )
    except DriveDocsError as exc:
        notes.append(f"drive: {exc}")
        en_title = candidate.en_title_airtable
        en_desc = candidate.en_description_airtable
        title_origin = "airtable" if en_title else None
        desc_origin = "airtable" if en_desc else None

    pairs: list[MetadataPair] = []
    if candidate.bg_title:
        if en_title and title_origin:
            pairs.append(
                MetadataPair(
                    kind="title",
                    en=en_title,
                    bg=candidate.bg_title,
                    video_title=candidate.title,
                    record_id=candidate.record_id,
                    record_type=candidate.record_type,
                    status=candidate.status,
                    source=candidate.source,
                    base_id=candidate.base_id,
                    table_name=candidate.table_name,
                    en_origin=title_origin,
                )
            )
        else:
            notes.append("missing EN title")
    if candidate.bg_description:
        if en_desc and desc_origin:
            pairs.append(
                MetadataPair(
                    kind="description",
                    en=en_desc,
                    bg=candidate.bg_description,
                    video_title=candidate.title,
                    record_id=candidate.record_id,
                    record_type=candidate.record_type,
                    status=candidate.status,
                    source=candidate.source,
                    base_id=candidate.base_id,
                    table_name=candidate.table_name,
                    en_origin=desc_origin,
                )
            )
        else:
            notes.append("missing EN description")
    return pairs, notes
