from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    catalog_record_to_airtable_fields,
    normalize_original_video_key,
    normalize_original_video_name_key,
    title_identity_keys,
)
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.workflow.status_history import record_status_history_from_snapshots

DEFAULT_BACKUP_DIR = Path("output") / "backups"
INCREMENTAL_OVERLAP = timedelta(minutes=15)
FULL_REFRESH_AFTER = timedelta(days=7)


def _full_refresh_requested() -> bool:
    value = os.getenv("AIRTABLE_FULL_REFRESH", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _modified_since_formula(since: datetime) -> str:
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f"IS_AFTER(LAST_MODIFIED_TIME(), '{stamp}')"


def merge_airtable_records(
    baseline: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace baseline rows by id and append records that were not in the snapshot."""
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in baseline:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            continue
        merged[record_id] = TableCache._copy_record(record)
        order.append(record_id)
    seen = set(order)
    for record in updates:
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id:
            continue
        merged[record_id] = TableCache._copy_record(record)
        if record_id not in seen:
            order.append(record_id)
            seen.add(record_id)
    return [merged[record_id] for record_id in order]


def _request_count(airtable: AirtableClient) -> int | None:
    count = getattr(airtable, "request_count", None)
    return count if isinstance(count, int) else None


def _load_table_records(
    airtable: AirtableClient,
    *,
    previous_records: list[dict[str, Any]] | None,
    previous_fetched_at: datetime | None,
) -> tuple[list[dict[str, Any]], str]:
    """Load the live table, reusing a recent snapshot when one exists."""
    now = datetime.now(timezone.utc)
    fetched_at = previous_fetched_at
    if fetched_at is not None and fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    snapshot_age = None if fetched_at is None else now - fetched_at.astimezone(timezone.utc)
    can_incremental = (
        not _full_refresh_requested()
        and bool(previous_records)
        and fetched_at is not None
        and snapshot_age is not None
        and snapshot_age <= FULL_REFRESH_AFTER
    )
    before = _request_count(airtable)
    if can_incremental and fetched_at is not None and previous_records is not None:
        cursor = fetched_at.astimezone(timezone.utc) - INCREMENTAL_OVERLAP
        try:
            changed = airtable.list_records(
                filter_formula=_modified_since_formula(cursor)
            )
        except AirtableError as exc:
            print(
                "Warning: incremental Airtable sync failed "
                f"({exc}); loading the full table"
            )
        else:
            used = _calls_since(airtable, before)
            call_note = f", {used} API call(s)" if used is not None else ""
            return merge_airtable_records(previous_records, changed), (
                f"incremental sync of {len(changed)} changed record(s) "
                f"since {cursor.isoformat()}{call_note}"
            )

    records = airtable.list_records()
    used = _calls_since(airtable, before)
    call_note = f", {used} API call(s)" if used is not None else ""
    if _full_refresh_requested():
        reason = "full refresh requested"
    elif snapshot_age is not None and snapshot_age > FULL_REFRESH_AFTER:
        reason = "snapshot older than 7 days"
    else:
        reason = "full table load"
    return records, f"{reason}{call_note}"


def _calls_since(airtable: AirtableClient, before: int | None) -> int | None:
    after = _request_count(airtable)
    if before is None or after is None:
        return None
    return after - before


class TableCache:
    """In-memory snapshot of an Airtable table for a single workflow run."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        *,
        fetched_at: datetime | None = None,
    ) -> None:
        self._records = [self._copy_record(record) for record in records]
        self.fetched_at = fetched_at or datetime.now(timezone.utc)

    @classmethod
    def from_backup_file(cls, path: Path) -> TableCache:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Backup file {path} must contain a JSON object")

        records = payload.get("records")
        if not isinstance(records, list):
            raise ValueError(f"Backup file {path} is missing a records array")

        fetched_at_raw = payload.get("fetched_at")
        fetched_at: datetime | None = None
        if isinstance(fetched_at_raw, str) and fetched_at_raw.strip():
            fetched_at = datetime.fromisoformat(fetched_at_raw)

        return cls(records, fetched_at=fetched_at)

    @property
    def backup_metadata(self) -> dict[str, Any]:
        return {
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat(),
            "record_count": len(self._records),
        }

    @classmethod
    def load(
        cls,
        airtable: AirtableClient,
        *,
        project_root: Path | None = None,
        backup: bool = True,
        backup_dir: Path | None = None,
        record_status_history: bool = True,
    ) -> TableCache:
        previous_records: list[dict[str, Any]] | None = None
        previous_fetched_at: datetime | None = None
        target_dir: Path | None = None
        latest_path: Path | None = None
        previous_path: Path | None = None

        if project_root is not None:
            target_dir = project_root / (backup_dir or DEFAULT_BACKUP_DIR)
            latest_path = target_dir / "airtable-latest.json"
            previous_path = target_dir / "airtable-previous.json"
            if latest_path.is_file():
                try:
                    previous_cache = cls.from_backup_file(latest_path)
                    previous_records = previous_cache.records
                    previous_fetched_at = previous_cache.fetched_at
                except ValueError as exc:
                    print(f"Warning: could not load previous backup from {latest_path}: {exc}")

        records, sync_note = _load_table_records(
            airtable,
            previous_records=previous_records,
            previous_fetched_at=previous_fetched_at,
        )
        cache = cls(records)

        if (
            record_status_history
            and project_root is not None
            and previous_records is not None
        ):
            events = record_status_history_from_snapshots(
                project_root=project_root,
                previous_records=previous_records,
                current_records=cache.records,
                detected_at=cache.fetched_at,
            )
            if events:
                print(f"Recorded {len(events)} status work event(s) in status history")

        if backup and project_root is not None and target_dir is not None:
            if latest_path is not None and previous_path is not None and latest_path.is_file():
                shutil.copy2(latest_path, previous_path)
            path = cache.write_backup(project_root, backup_dir=backup_dir)
            print(
                f"Cached {len(cache._records)} Airtable record(s) ({sync_note}); "
                f"backup written to {path}"
            )
        else:
            print(f"Cached {len(cache._records)} Airtable record(s) ({sync_note})")
        return cache

    @property
    def records(self) -> list[dict[str, Any]]:
        return self._records

    def filter_records(
        self,
        predicate: Callable[[dict[str, Any]], bool],
    ) -> list[dict[str, Any]]:
        return [record for record in self._records if predicate(record)]

    def get(self, record_id: str) -> dict[str, Any] | None:
        for record in self._records:
            if record.get("id") == record_id:
                return record
        return None

    def existing_title_keys(self) -> set[str]:
        """Typed title identity keys (``{type}\\t{title}``)."""
        keys: set[str] = set()
        for record in self._records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            keys.update(
                title_identity_keys(
                    fields.get(FIELD_TITLE),
                    fields.get(FIELD_TYPE),
                )
            )
        return keys

    def existing_titles(self) -> set[str]:
        """Backward-compatible alias for :meth:`existing_title_keys`."""
        return self.existing_title_keys()

    def existing_video_folder_ids(self) -> set[str]:
        folder_ids: set[str] = set()
        for record in self._records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            link = fields.get(FIELD_VIDEO_FOLDER)
            if not isinstance(link, str) or not link.strip():
                continue
            folder_id = extract_drive_folder_id(link)
            if folder_id:
                folder_ids.add(folder_id)
        return folder_ids

    def existing_original_video_names(self) -> set[str]:
        names: set[str] = set()
        for record in self._records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            key = normalize_original_video_name_key(
                fields.get(FIELD_ORIGINAL_VIDEO_NAME)
            )
            if key:
                names.add(key)
        return names

    def existing_original_video_keys(self) -> set[str]:
        keys: set[str] = set()
        for record in self._records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            key = normalize_original_video_key(fields.get(FIELD_ORIGINAL_VIDEO))
            if key:
                keys.add(key)
        return keys

    def update_fields(self, record_id: str, field_updates: dict[str, Any]) -> None:
        record = self.get(record_id)
        if record is None:
            return
        fields = record.get("fields")
        if isinstance(fields, dict):
            fields.update(field_updates)

    def add_record(self, record: dict[str, Any]) -> None:
        self._records.append(self._copy_record(record))

    def register_created_from_catalog(
        self,
        catalog_records: list[dict[str, Any]],
        record_ids: list[str],
    ) -> None:
        for catalog_record, record_id in zip(catalog_records, record_ids, strict=True):
            fields = catalog_record_to_airtable_fields(catalog_record)
            per_record_extra = catalog_record.get("_airtable_fields")
            if isinstance(per_record_extra, dict):
                fields.update(per_record_extra)
            self.add_record({"id": record_id, "fields": fields})

    def write_backup(
        self,
        project_root: Path,
        *,
        backup_dir: Path | None = None,
    ) -> Path:
        target_dir = project_root / (backup_dir or DEFAULT_BACKUP_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)

        stamp = self.fetched_at.astimezone(timezone.utc).strftime("%Y-%m-%d")
        dated_path = target_dir / f"airtable-{stamp}.json"
        latest_path = target_dir / "airtable-latest.json"
        payload = {
            "fetched_at": self.fetched_at.astimezone(timezone.utc).isoformat(),
            "record_count": len(self._records),
            "records": self._records,
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2)
        dated_path.write_text(encoded, encoding="utf-8")
        latest_path.write_text(encoded, encoding="utf-8")
        return dated_path

    @staticmethod
    def _copy_record(record: dict[str, Any]) -> dict[str, Any]:
        copied = dict(record)
        fields = copied.get("fields")
        if isinstance(fields, dict):
            copied["fields"] = dict(fields)
        return copied
