from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from catalog_parser.airtable import FIELD_TITLE


@dataclass(frozen=True)
class RestoreUpdate:
    record_id: str
    title: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class RestoreCreate:
    title: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class RestoreOrphan:
    record_id: str
    title: str


@dataclass(frozen=True)
class RestorePlan:
    backup_fetched_at: str | None
    backup_count: int
    live_count: int
    updates: tuple[RestoreUpdate, ...]
    creates: tuple[RestoreCreate, ...]
    orphans: tuple[RestoreOrphan, ...]

    @property
    def changed_record_count(self) -> int:
        return len(self.updates) + len(self.creates) + len(self.orphans)


def fields_equal(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(
        right,
        sort_keys=True,
        ensure_ascii=False,
    )


def record_title(record: dict[str, Any]) -> str:
    fields = record.get("fields")
    if isinstance(fields, dict):
        title = fields.get(FIELD_TITLE)
        if isinstance(title, str) and title.strip():
            return title.strip()
    record_id = record.get("id")
    return record_id if isinstance(record_id, str) else "(unknown)"


def field_updates_from_backup(
    backup_fields: dict[str, Any],
    live_fields: dict[str, Any],
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    for field_name, backup_value in backup_fields.items():
        live_value = live_fields.get(field_name)
        if not fields_equal(backup_value, live_value):
            updates[field_name] = backup_value
    return updates


def build_restore_plan(
    backup_records: list[dict[str, Any]],
    live_records: list[dict[str, Any]],
    *,
    backup_fetched_at: str | None = None,
) -> RestorePlan:
    live_by_id = {
        record["id"]: record
        for record in live_records
        if isinstance(record.get("id"), str)
    }
    backup_ids: set[str] = set()

    updates: list[RestoreUpdate] = []
    creates: list[RestoreCreate] = []

    for backup_record in backup_records:
        record_id = backup_record.get("id")
        backup_fields = backup_record.get("fields")
        if not isinstance(record_id, str) or not isinstance(backup_fields, dict):
            continue
        backup_ids.add(record_id)
        title = record_title(backup_record)

        live_record = live_by_id.get(record_id)
        if live_record is None:
            creates.append(RestoreCreate(title=title, fields=dict(backup_fields)))
            continue

        live_fields = live_record.get("fields")
        if not isinstance(live_fields, dict):
            live_fields = {}
        changed_fields = field_updates_from_backup(backup_fields, live_fields)
        if changed_fields:
            updates.append(
                RestoreUpdate(
                    record_id=record_id,
                    title=title,
                    fields=changed_fields,
                )
            )

    orphans = [
        RestoreOrphan(record_id=record_id, title=record_title(live_record))
        for record_id, live_record in live_by_id.items()
        if record_id not in backup_ids
    ]

    return RestorePlan(
        backup_fetched_at=backup_fetched_at,
        backup_count=len(backup_records),
        live_count=len(live_records),
        updates=tuple(updates),
        creates=tuple(creates),
        orphans=tuple(orphans),
    )
