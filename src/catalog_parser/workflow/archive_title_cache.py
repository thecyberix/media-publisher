from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_parser.airtable import AirtableArchiveSource, AirtableClient
from catalog_parser.workflow.table_cache import DEFAULT_BACKUP_DIR

DEFAULT_CACHE_FILENAME = "airtable-archive-titles.json"
# v1 stored title strings only. v2 stores every ingest identity set from one
# list of each archive table. A v1 file is ignored so the next ingest refetches
# once; later runs keep using the file and make no archive API calls.
CACHE_VERSION = 2

_PROCESS_CACHE: dict[str, ArchiveIngestIdentity] = {}
_UNVERIFIED_FILE_CACHE: ArchiveIngestIdentity | None = None
_UNVERIFIED_FILE_CACHE_PATH: str | None = None


@dataclass(frozen=True)
class ArchiveIngestIdentity:
    title_keys: frozenset[str]
    folder_ids: frozenset[str]
    original_video_names: frozenset[str]
    original_video_keys: frozenset[str]

    @classmethod
    def empty(cls) -> ArchiveIngestIdentity:
        return cls(frozenset(), frozenset(), frozenset(), frozenset())


def archive_cache_enabled() -> bool:
    value = os.getenv("AIRTABLE_ARCHIVE_CACHE", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def archive_cache_refresh_requested() -> bool:
    value = os.getenv("AIRTABLE_ARCHIVE_CACHE_REFRESH", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def archive_cache_path(project_root: Path) -> Path:
    return project_root / DEFAULT_BACKUP_DIR / DEFAULT_CACHE_FILENAME


def _sources_fingerprint(sources: list[AirtableArchiveSource]) -> str:
    payload = []
    for source in sources:
        item = asdict(source)
        item["title_fields"] = list(source.title_fields)
        item["identity_fields"] = list(source.identity_fields)
        payload.append(item)
    return json.dumps(payload, sort_keys=True)


def _parse_fetched_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _string_tuple(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Archive identity cache {label} must be an array")
    fields = tuple(str(item) for item in value if str(item).strip())
    return fields


def _sources_from_payload(payload: dict[str, Any]) -> list[AirtableArchiveSource]:
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("Archive identity cache is missing a sources array")

    sources: list[AirtableArchiveSource] = []
    for item in raw_sources:
        if not isinstance(item, dict):
            raise ValueError("Archive identity cache sources must be objects")
        title_fields = item.get("title_fields")
        if not isinstance(title_fields, list) or not title_fields:
            title_field = item.get("title_field")
            if isinstance(title_field, str) and title_field.strip():
                title_fields = [title_field.strip()]
            else:
                raise ValueError("Archive identity cache source is missing title_fields")
        sources.append(
            AirtableArchiveSource(
                base_id=str(item["base_id"]),
                table_name=str(item["table_name"]),
                title_fields=tuple(str(field) for field in title_fields if str(field).strip()),
                identity_fields=_string_tuple(
                    item.get("identity_fields", []),
                    label="identity_fields",
                ),
                title_from_original_video_name=bool(
                    item.get("title_from_original_video_name", False)
                ),
            )
        )
    return sources


def _string_set(payload: dict[str, Any], key: str) -> frozenset[str]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"Archive identity cache is missing {key}")
    values: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item.strip():
            values.add(item.strip())
    return frozenset(values)


def _identity_from_payload(payload: dict[str, Any]) -> ArchiveIngestIdentity | None:
    if payload.get("version") != CACHE_VERSION:
        return None
    if _parse_fetched_at(payload.get("fetched_at")) is None:
        return None
    return ArchiveIngestIdentity(
        title_keys=_string_set(payload, "title_keys"),
        folder_ids=_string_set(payload, "folder_ids"),
        original_video_names=_string_set(payload, "original_video_names"),
        original_video_keys=_string_set(payload, "original_video_keys"),
    )


def _read_payload(cache_path: Path) -> dict[str, Any] | None:
    if not cache_path.is_file():
        return None
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Archive identity cache {cache_path} must contain a JSON object")
    return payload


def read_archive_identity_cache(
    cache_path: Path,
    *,
    sources: list[AirtableArchiveSource],
) -> ArchiveIngestIdentity | None:
    payload = _read_payload(cache_path)
    if payload is None:
        return None
    if _sources_from_payload(payload) != sources:
        return None
    return _identity_from_payload(payload)


def peek_archive_identity_cache(cache_path: Path) -> ArchiveIngestIdentity | None:
    """Load a v2 cache without resolving archive bases (no Airtable calls)."""
    global _UNVERIFIED_FILE_CACHE, _UNVERIFIED_FILE_CACHE_PATH
    path_key = str(cache_path)
    if (
        _UNVERIFIED_FILE_CACHE is not None
        and _UNVERIFIED_FILE_CACHE_PATH == path_key
    ):
        return _UNVERIFIED_FILE_CACHE

    if not cache_path.is_file():
        return None

    try:
        payload = _read_payload(cache_path)
        if payload is None:
            return None
        identity = _identity_from_payload(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if identity is None:
        return None
    _UNVERIFIED_FILE_CACHE = identity
    _UNVERIFIED_FILE_CACHE_PATH = path_key
    return identity


def write_archive_identity_cache(
    cache_path: Path,
    *,
    sources: list[AirtableArchiveSource],
    identity: ArchiveIngestIdentity,
    fetched_at: datetime | None = None,
) -> Path:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "fetched_at": (fetched_at or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .isoformat(),
        "title_count": len(identity.title_keys),
        "folder_count": len(identity.folder_ids),
        "original_video_name_count": len(identity.original_video_names),
        "original_video_key_count": len(identity.original_video_keys),
        "sources": [
            {
                **asdict(source),
                "title_fields": list(source.title_fields),
                "identity_fields": list(source.identity_fields),
            }
            for source in sources
        ],
        "title_keys": sorted(identity.title_keys),
        "folder_ids": sorted(identity.folder_ids),
        "original_video_names": sorted(identity.original_video_names),
        "original_video_keys": sorted(identity.original_video_keys),
    }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return cache_path


def fetch_archive_identity(
    airtable: AirtableClient,
    sources: list[AirtableArchiveSource],
) -> ArchiveIngestIdentity:
    """List each archive table once. Extra identity columns share that page."""
    titles: set[str] = set()
    folder_ids: set[str] = set()
    names: set[str] = set()
    video_keys: set[str] = set()
    for source in sources:
        if not source.identity_fields:
            continue
        row_titles, row_folders, row_names, row_videos = airtable.list_table_ingest_identity(
            base_id=source.base_id,
            table_name=source.table_name,
            fields=source.identity_fields,
            title_from_original_video_name=source.title_from_original_video_name,
        )
        titles.update(row_titles)
        folder_ids.update(row_folders)
        names.update(row_names)
        video_keys.update(row_videos)
    return ArchiveIngestIdentity(
        frozenset(titles),
        frozenset(folder_ids),
        frozenset(names),
        frozenset(video_keys),
    )


def _remember_file_cache(cache_path: Path, identity: ArchiveIngestIdentity) -> None:
    global _UNVERIFIED_FILE_CACHE, _UNVERIFIED_FILE_CACHE_PATH
    _UNVERIFIED_FILE_CACHE = identity
    _UNVERIFIED_FILE_CACHE_PATH = str(cache_path)


def load_archive_identity(
    airtable: AirtableClient,
    sources: list[AirtableArchiveSource] | None,
    *,
    project_root: Path,
    records: list[dict[str, Any]] | None = None,
    skip_base_id: str | None = None,
    skip_table_name: str | None = None,
    force_refresh: bool = False,
) -> ArchiveIngestIdentity:
    """Return archive ingest identity, using the file cache whenever it is v2.

    A warm cache performs no Airtable calls. A miss lists each archive table
    once (100 records per request) and writes the cache for later runs.
    """
    refresh = force_refresh or archive_cache_refresh_requested()
    cache_path = archive_cache_path(project_root)

    if sources is None and archive_cache_enabled() and not refresh:
        peeked = peek_archive_identity_cache(cache_path)
        if peeked is not None:
            print(
                "Using cached archive identity for ingest duplicate checks "
                f"({len(peeked.title_keys)} title key(s), "
                f"{len(peeked.folder_ids)} folder id(s), "
                f"{len(peeked.original_video_names)} original name(s), "
                f"{len(peeked.original_video_keys)} video key(s) from {cache_path})"
            )
            return peeked

    if sources is None:
        from catalog_parser.workflow.archive_sources import resolve_archive_sources

        sources = resolve_archive_sources(airtable, records=records)

    selected = [
        source
        for source in sources
        if not (
            skip_base_id
            and skip_table_name
            and source.base_id == skip_base_id
            and source.table_name == skip_table_name
        )
    ]
    if not selected:
        return ArchiveIngestIdentity.empty()

    fingerprint = _sources_fingerprint(selected)
    if not refresh and fingerprint in _PROCESS_CACHE:
        return _PROCESS_CACHE[fingerprint]

    identity: ArchiveIngestIdentity | None = None
    if archive_cache_enabled() and not refresh:
        try:
            identity = read_archive_identity_cache(cache_path, sources=selected)
        except ValueError as exc:
            print(f"Warning: ignoring invalid archive identity cache at {cache_path}: {exc}")

    if identity is None:
        before = airtable.request_count
        identity = fetch_archive_identity(airtable, selected)
        calls = airtable.request_count - before
        if archive_cache_enabled():
            path = write_archive_identity_cache(
                cache_path,
                sources=selected,
                identity=identity,
            )
            print(
                "Cached archive identity for ingest duplicate checks "
                f"({len(identity.title_keys)} title key(s), "
                f"{len(identity.folder_ids)} folder id(s), "
                f"{len(identity.original_video_names)} original name(s), "
                f"{len(identity.original_video_keys)} video key(s)) "
                f"in {calls} Airtable API call(s); backup written to {path}"
            )
    else:
        print(
            "Using cached archive identity for ingest duplicate checks "
            f"({len(identity.title_keys)} title key(s) from {cache_path})"
        )

    _PROCESS_CACHE[fingerprint] = identity
    _remember_file_cache(cache_path, identity)
    return identity
