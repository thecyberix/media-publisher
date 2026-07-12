from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_TITLE,
    catalog_record_to_airtable_fields,
    normalize_title,
)
from catalog_parser.workflow.status_history import record_status_history_from_snapshots

DEFAULT_BACKUP_DIR = Path("output") / "backups"


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
        target_dir: Path | None = None
        latest_path: Path | None = None
        previous_path: Path | None = None

        if project_root is not None:
            target_dir = project_root / (backup_dir or DEFAULT_BACKUP_DIR)
            latest_path = target_dir / "airtable-latest.json"
            previous_path = target_dir / "airtable-previous.json"
            if latest_path.is_file():
                try:
                    previous_records = cls.from_backup_file(latest_path).records
                except ValueError as exc:
                    print(f"Warning: could not load previous backup from {latest_path}: {exc}")

        records = airtable.list_records()
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
                f"Cached {len(cache._records)} Airtable record(s); "
                f"backup written to {path}"
            )
        else:
            print(f"Cached {len(cache._records)} Airtable record(s)")
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

    def existing_titles(self) -> set[str]:
        titles: set[str] = set()
        for record in self._records:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            title = normalize_title(fields.get(FIELD_TITLE))
            if title:
                titles.add(title)
        return titles

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
