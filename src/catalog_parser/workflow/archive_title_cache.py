from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_parser.airtable import AirtableArchiveSource, AirtableClient
from catalog_parser.workflow.table_cache import DEFAULT_BACKUP_DIR

DEFAULT_CACHE_FILENAME = "airtable-archive-titles.json"

_PROCESS_CACHE: dict[str, frozenset[str]] = {}


def archive_cache_enabled() -> bool:
    value = os.getenv("AIRTABLE_ARCHIVE_CACHE", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def archive_cache_refresh_requested() -> bool:
    value = os.getenv("AIRTABLE_ARCHIVE_CACHE_REFRESH", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def archive_cache_path(project_root: Path) -> Path:
    return project_root / DEFAULT_BACKUP_DIR / DEFAULT_CACHE_FILENAME


def _sources_fingerprint(sources: list[AirtableArchiveSource]) -> str:
    payload = [asdict(source) for source in sources]
    return json.dumps(payload, sort_keys=True)


def _parse_fetched_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sources_from_payload(payload: dict[str, Any]) -> list[AirtableArchiveSource]:
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("Archive title cache is missing a sources array")

    sources: list[AirtableArchiveSource] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            raise ValueError("Archive title cache sources must be objects")
        sources.append(
            AirtableArchiveSource(
                base_id=str(item["base_id"]),
                table_name=str(item["table_name"]),
                title_fields=_title_fields_from_payload(item),
            )
        )
    return sources


def _title_fields_from_payload(item: dict[str, Any]) -> tuple[str, ...]:
    raw_fields = item.get("title_fields")
    if isinstance(raw_fields, list):
        fields = tuple(str(field) for field in raw_fields if str(field).strip())
        if fields:
            return fields
    title_field = item.get("title_field")
    if isinstance(title_field, str) and title_field.strip():
        return (title_field.strip(),)
    raise ValueError("Archive title cache source is missing title_fields")


def read_archive_title_cache(
    cache_path: Path,
    *,
    sources: list[AirtableArchiveSource],
) -> set[str] | None:
    if not cache_path.is_file():
        return None

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Archive title cache {cache_path} must contain a JSON object")

    cached_sources = _sources_from_payload(payload)
    if cached_sources != sources:
        return None

    if _parse_fetched_at(payload.get("fetched_at")) is None:
        return None

    titles = payload.get("titles")
    if not isinstance(titles, list):
        raise ValueError(f"Archive title cache {cache_path} is missing a titles array")

    normalized: set[str] = set()
    for title in titles:
        if isinstance(title, str) and title.strip():
            normalized.add(title.strip())
    return normalized


def write_archive_title_cache(
    cache_path: Path,
    *,
    sources: list[AirtableArchiveSource],
    titles: set[str],
    fetched_at: datetime | None = None,
) -> Path:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": (fetched_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
        "title_count": len(titles),
        "sources": [
            {**asdict(source), "title_fields": list(source.title_fields)}
            for source in sources
        ],
        "titles": sorted(titles),
    }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cache_path


def fetch_archive_titles(
    airtable: AirtableClient,
    sources: list[AirtableArchiveSource],
) -> set[str]:
    titles: set[str] = set()
    for source in sources:
        titles.update(
            airtable.list_title_variants(
                title_fields=source.title_fields,
                base_id=source.base_id,
                table_name=source.table_name,
            )
        )
    return titles


def load_archive_titles(
    airtable: AirtableClient,
    sources: list[AirtableArchiveSource],
    *,
    project_root: Path,
    force_refresh: bool = False,
) -> set[str]:
    if not sources:
        return set()

    refresh = force_refresh or archive_cache_refresh_requested()
    fingerprint = _sources_fingerprint(sources)
    if not refresh and fingerprint in _PROCESS_CACHE:
        return set(_PROCESS_CACHE[fingerprint])

    cache_path = archive_cache_path(project_root)
    titles: set[str] | None = None
    if archive_cache_enabled() and not refresh:
        try:
            titles = read_archive_title_cache(cache_path, sources=sources)
        except ValueError as exc:
            print(f"Warning: ignoring invalid archive title cache at {cache_path}: {exc}")

    if titles is None:
        titles = fetch_archive_titles(airtable, sources)
        if archive_cache_enabled():
            path = write_archive_title_cache(cache_path, sources=sources, titles=titles)
            print(
                f"Cached {len(titles)} archive title(s) for ingest duplicate checks; "
                f"backup written to {path}"
            )
    else:
        print(
            f"Using cached archive titles for ingest duplicate checks "
            f"({len(titles)} title(s) from {cache_path})"
        )

    frozen = frozenset(titles)
    _PROCESS_CACHE[fingerprint] = frozen
    return set(frozen)
