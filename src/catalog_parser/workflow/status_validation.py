from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from catalog_parser.airtable import (
    FIELD_ORIGINAL_VIDEO_DESCRIPTION,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
    AirtableClient,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.table_cache import TableCache

MISSING_TITLE_COMMENT = "Missing title translation"
MISSING_DESCRIPTION_COMMENT = "Missing description translation"
MISSING_CAPTION_COMMENT = "Missing caption translation"

TARGET_STATUSES = (STATUS_TRANSLATION_DONE, STATUS_EDITING_DONE)


@dataclass(frozen=True)
class StatusRevertAction:
    record_id: str
    title: str
    previous_status: str
    attempted_status: str
    comments: tuple[str, ...]


def _field_text(fields: dict[str, Any], name: str) -> str | None:
    value = fields.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _has_attachment(fields: dict[str, Any], name: str) -> bool:
    value = fields.get(name)
    return isinstance(value, list) and bool(value)


def missing_translation_requirements(fields: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not _field_text(fields, FIELD_VIDEO_NAME_TRANSLATED):
        missing.append(MISSING_TITLE_COMMENT)
    if _field_text(fields, FIELD_ORIGINAL_VIDEO_DESCRIPTION) and not _field_text(
        fields, FIELD_VIDEO_DESCRIPTION_TRANSLATED
    ):
        missing.append(MISSING_DESCRIPTION_COMMENT)
    if _has_attachment(fields, FIELD_ORIGINAL_VIDEO_THUMBNAIL) and not _field_text(
        fields, FIELD_VIDEO_CAPTION_TRANSLATED
    ):
        missing.append(MISSING_CAPTION_COMMENT)
    return missing


def detect_invalid_status_transitions(
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
) -> list[StatusRevertAction]:
    previous_by_id = {
        record["id"]: record
        for record in previous_records
        if isinstance(record.get("id"), str)
    }
    actions: list[StatusRevertAction] = []

    for current in current_records:
        record_id = current.get("id")
        current_fields = current.get("fields")
        if not isinstance(record_id, str) or not isinstance(current_fields, dict):
            continue

        record_type = current_fields.get("Type")
        if record_type not in {TYPE_VIDEO, TYPE_REEL}:
            continue

        previous = previous_by_id.get(record_id)
        previous_fields = previous.get("fields", {}) if isinstance(previous, dict) else {}
        if not isinstance(previous_fields, dict):
            previous_fields = {}

        old_status = _field_text(previous_fields, FIELD_STATUS)
        new_status = _field_text(current_fields, FIELD_STATUS)
        if not new_status or old_status == new_status:
            continue
        if new_status not in TARGET_STATUSES:
            continue

        missing = missing_translation_requirements(current_fields)
        if not missing:
            continue

        revert_status = old_status or STATUS_TODO
        title = _field_text(current_fields, FIELD_TITLE) or record_id
        actions.append(
            StatusRevertAction(
                record_id=record_id,
                title=title,
                previous_status=revert_status,
                attempted_status=new_status,
                comments=tuple(missing),
            )
        )

    return actions


def apply_status_reverts(
    *,
    airtable: AirtableClient,
    table_cache: TableCache,
    actions: list[StatusRevertAction],
    dry_run: bool = False,
) -> int:
    if not actions:
        return 0

    print(f"Status validation: reverting {len(actions)} invalid transition(s)")
    applied = 0
    for action in actions:
        comment_summary = "; ".join(action.comments)
        print(
            f"  - {action.title}: {action.attempted_status} -> {action.previous_status} "
            f"({comment_summary})"
        )
        if dry_run:
            applied += 1
            continue

        airtable.update_record_fields(
            action.record_id,
            {FIELD_STATUS: action.previous_status},
        )
        for comment in action.comments:
            airtable.create_record_comment(action.record_id, comment)
        table_cache.update_fields(
            action.record_id,
            {FIELD_STATUS: action.previous_status},
        )
        applied += 1

    return applied
