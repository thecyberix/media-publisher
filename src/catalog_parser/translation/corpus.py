from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    AirtableArchiveSource,
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TYPE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_SYNC_DONE,
)
from catalog_parser.smartcat import parse_pkg_sm_link, parse_smartcat_resource_link
from catalog_parser.workflow.archive_sources import (
    archive_pointers_from_records,
    resolve_archive_sources,
)

# Completed translations only — excludes "2. Translation done" (not edited yet).
# Published rows in archives use numbered labels like "6. Done & Published".
CORPUS_STATUSES_EXACT = (
    STATUS_EDITING_DONE,
    STATUS_SYNC_DONE,
)

DEFAULT_TITLE_FIELDS = (FIELD_TITLE, FIELD_ORIGINAL_VIDEO_NAME)

DEFAULT_HOLDOUT_COUNT = 30
DEFAULT_HOLDOUT_SEED = "media-publisher-corpus-holdout"
DEFAULT_HOLDOUT_PATH = Path("data/corpus/holdout_titles.json")


@dataclass(frozen=True)
class CorpusCandidate:
    record_id: str
    title: str
    status: str
    record_type: str
    smartcat_link: str
    translated_title: str | None
    source: str
    base_id: str
    table_name: str


@dataclass(frozen=True)
class CorpusSelection:
    export_candidates: list[CorpusCandidate]
    holdout_candidates: list[CorpusCandidate]
    skipped_translation_done: int = 0


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = value.strip()
    return text or None


def is_parseable_smartcat_link(value: str) -> bool:
    lowered = value.casefold()
    if "smartcat.com" not in lowered and "smartcat.ai" not in lowered:
        return False
    return (
        parse_pkg_sm_link(value) is not None
        or parse_smartcat_resource_link(value) is not None
    )


def build_corpus_status_filter(*, include_in_progress: bool = True) -> str:
    checks = [f'FIND("Done & Published", {{{FIELD_STATUS}}} & "")']
    if include_in_progress:
        checks.extend(
            f'{{{FIELD_STATUS}}}="{status}"' for status in CORPUS_STATUSES_EXACT
        )
    return f"OR({','.join(checks)})"


def build_corpus_query_filter(*, include_in_progress: bool = True) -> str:
    return (
        f"AND({build_corpus_status_filter(include_in_progress=include_in_progress)}, "
        f'FIND("smartcat", {{{FIELD_TRANSLATION_RESOURCES}}} & ""))'
    )


def resolve_record_title(
    fields: dict[str, Any],
    *,
    title_fields: tuple[str, ...] = DEFAULT_TITLE_FIELDS,
) -> str | None:
    for field_name in title_fields:
        title = _field_text(fields.get(field_name))
        if title:
            return title
    return None


def records_to_candidates(
    records: list[dict[str, Any]],
    *,
    source: str,
    base_id: str,
    table_name: str,
    title_fields: tuple[str, ...] = DEFAULT_TITLE_FIELDS,
) -> list[CorpusCandidate]:
    candidates: list[CorpusCandidate] = []

    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        smartcat_link = _field_text(fields.get(FIELD_TRANSLATION_RESOURCES))
        title = resolve_record_title(fields, title_fields=title_fields)
        if not smartcat_link or not title or not is_parseable_smartcat_link(smartcat_link):
            continue
        candidates.append(
            CorpusCandidate(
                record_id=str(record.get("id") or ""),
                title=title,
                status=_field_text(fields.get(FIELD_STATUS)) or "",
                record_type=_field_text(fields.get(FIELD_TYPE)) or "",
                smartcat_link=smartcat_link,
                translated_title=_field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED)),
                source=source,
                base_id=base_id,
                table_name=table_name,
            )
        )

    return candidates


def list_candidates_for_table(
    airtable: AirtableClient,
    *,
    source: str,
    base_id: str | None = None,
    table_name: str | None = None,
    title_fields: tuple[str, ...] = DEFAULT_TITLE_FIELDS,
    include_in_progress: bool = True,
) -> list[CorpusCandidate]:
    resolved_base_id = (base_id or airtable.base_id).strip()
    resolved_table_name = (table_name or airtable.table_name).strip()
    records = airtable.list_records(
        filter_formula=build_corpus_query_filter(include_in_progress=include_in_progress),
        base_id=resolved_base_id,
        table_name=resolved_table_name,
    )
    return records_to_candidates(
        records,
        source=source,
        base_id=resolved_base_id,
        table_name=resolved_table_name,
        title_fields=title_fields,
    )


def archive_year_by_base_id(airtable: AirtableClient) -> dict[str, str]:
    records = airtable.list_records()
    pointers = archive_pointers_from_records(records)
    if not pointers:
        return {}

    bases = airtable.list_accessible_bases()
    mapping: dict[str, str] = {}
    for year, _invite_url in pointers:
        needle = f"archive {year}".casefold()
        for base in bases:
            name = str(base.get("name") or "").casefold()
            if needle not in name:
                continue
            base_id = str(base.get("id") or "").strip()
            if base_id:
                mapping[base_id] = year
            break
    return mapping


def list_archive_candidates(
    airtable: AirtableClient,
    *,
    archive_sources: list[AirtableArchiveSource] | None = None,
    year_by_base_id: dict[str, str] | None = None,
) -> list[CorpusCandidate]:
    if archive_sources is None:
        archive_sources = resolve_archive_sources(airtable)
    if year_by_base_id is None:
        year_by_base_id = archive_year_by_base_id(airtable)

    candidates: list[CorpusCandidate] = []
    for source in archive_sources:
        year = year_by_base_id.get(source.base_id, "archive")
        label = f"{year} archive"
        candidates.extend(
            list_candidates_for_table(
                airtable,
                source=label,
                base_id=source.base_id,
                table_name=source.table_name,
                title_fields=source.title_fields,
                include_in_progress=False,
            )
        )
    return candidates


def select_holdout_titles(
    candidates: list[CorpusCandidate],
    *,
    holdout_count: int,
    seed: str = DEFAULT_HOLDOUT_SEED,
) -> set[str]:
    if holdout_count <= 0 or not candidates:
        return set()

    unique_by_title = {candidate.title: candidate for candidate in candidates}
    ranked = sorted(
        unique_by_title.values(),
        key=lambda candidate: (
            hashlib.sha256(f"{seed}:{candidate.title}".encode("utf-8")).hexdigest(),
            candidate.title.casefold(),
        ),
    )
    return {candidate.title for candidate in ranked[:holdout_count]}


def split_current_table_holdout(
    current_candidates: list[CorpusCandidate],
    *,
    holdout_count: int,
    seed: str = DEFAULT_HOLDOUT_SEED,
) -> tuple[list[CorpusCandidate], list[CorpusCandidate]]:
    holdout_titles = select_holdout_titles(
        current_candidates,
        holdout_count=holdout_count,
        seed=seed,
    )
    export_rows: list[CorpusCandidate] = []
    holdout_rows: list[CorpusCandidate] = []
    for candidate in current_candidates:
        if candidate.title in holdout_titles:
            holdout_rows.append(candidate)
        else:
            export_rows.append(candidate)
    return export_rows, holdout_rows


def _corpus_type_rank(record_type: str) -> int:
    """Prefer full videos over shorts/reels when titles collide."""
    normalized = record_type.strip().casefold()
    if normalized == "video":
        return 0
    if normalized == "short":
        return 1
    if normalized == "reel":
        return 2
    return 3


def dedupe_candidates(candidates: list[CorpusCandidate]) -> list[CorpusCandidate]:
    """Keep one row per title.

    Preference order:
    1. Video over Short over Reel (same-name reel is usually a clip of the video)
    2. Archive over current-year table
    3. Stable record id
    """

    def priority(candidate: CorpusCandidate) -> tuple[int, int, str, str]:
        archive_rank = 0 if candidate.source.endswith(" archive") else 1
        return (
            _corpus_type_rank(candidate.record_type),
            archive_rank,
            candidate.title.casefold(),
            candidate.record_id,
        )

    best_by_title: dict[str, CorpusCandidate] = {}
    for candidate in sorted(candidates, key=priority):
        best_by_title.setdefault(candidate.title, candidate)
    return sorted(best_by_title.values(), key=lambda item: (item.source, item.title.casefold()))


def build_corpus_selection(
    airtable: AirtableClient,
    *,
    current_year: str,
    holdout_count: int = DEFAULT_HOLDOUT_COUNT,
    holdout_seed: str = DEFAULT_HOLDOUT_SEED,
    include_archives: bool = True,
    include_current: bool = True,
) -> CorpusSelection:
    archive_candidates: list[CorpusCandidate] = []
    if include_archives:
        archive_candidates = list_archive_candidates(airtable)

    current_candidates: list[CorpusCandidate] = []
    holdout_candidates: list[CorpusCandidate] = []
    if include_current:
        current_candidates = dedupe_candidates(
            list_candidates_for_table(
                airtable,
                source=str(current_year),
            )
        )
        current_export, holdout_candidates = split_current_table_holdout(
            current_candidates,
            holdout_count=holdout_count,
            seed=holdout_seed,
        )
    else:
        current_export = []

    export_candidates = dedupe_candidates([*archive_candidates, *current_export])
    return CorpusSelection(
        export_candidates=export_candidates,
        holdout_candidates=sorted(holdout_candidates, key=lambda item: item.title.casefold()),
    )


def write_holdout_manifest(
    path: Path,
    holdout_candidates: list[CorpusCandidate],
    *,
    current_year: str,
    holdout_count: int,
    holdout_seed: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current_year": current_year,
        "holdout_count": holdout_count,
        "holdout_seed": holdout_seed,
        "reserved_for": "AI translation verification",
        "generated_at": datetime.now().astimezone().isoformat(),
        "videos": [asdict(candidate) for candidate in holdout_candidates],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def default_current_year() -> str:
    return str(datetime.now().year)


def probe_corpus_sources(airtable: AirtableClient, *, current_year: str) -> None:
    archive_sources = resolve_archive_sources(airtable)
    print(f"Archive sources resolved: {len(archive_sources)}")
    for source in archive_sources:
        rows = list_candidates_for_table(
            airtable,
            source=f"{source.base_id}",
            base_id=source.base_id,
            table_name=source.table_name,
            title_fields=source.title_fields,
            include_in_progress=False,
        )
        print(
            f"  {source.table_name!r}: {len(rows)} published candidate(s) "
            f"(title fields: {', '.join(source.title_fields)})"
        )

    current_rows = list_candidates_for_table(airtable, source=current_year)
    print(f"Current table ({current_year}): {len(current_rows)} candidate(s)")
