from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_DURATION,
    FIELD_EDITOR,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO

DEFAULT_HISTORY_PATH = Path("output") / "workflow" / "status_history.json"

WorkKind = Literal["translator", "editor"]


@dataclass(frozen=True)
class StatusWorkEvent:
    record_id: str
    record_title: str
    record_type: str
    duration_seconds: int
    kind: WorkKind
    participant_name: str
    from_status: str | None
    to_status: str
    detected_at: str


def _field_text(fields: dict[str, Any], name: str) -> str | None:
    value = fields.get(name)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _record_title(fields: dict[str, Any], record_id: str) -> str:
    for field_name in (FIELD_TITLE, FIELD_ORIGINAL_VIDEO_NAME):
        title = _field_text(fields, field_name)
        if title:
            return title
    return record_id


def _has_combined_media(fields: dict[str, Any]) -> bool:
    combined = fields.get(FIELD_COMBINED_MEDIA_FILE)
    if combined is None:
        return False
    if not isinstance(combined, str):
        combined = str(combined)
    return bool(combined.strip())


def detect_status_work_events(
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
    *,
    detected_at: datetime,
) -> list[StatusWorkEvent]:
    previous_by_id = {
        record["id"]: record
        for record in previous_records
        if isinstance(record.get("id"), str)
    }
    detected_at_text = detected_at.astimezone(timezone.utc).isoformat()
    events: list[StatusWorkEvent] = []

    for current in current_records:
        record_id = current.get("id")
        current_fields = current.get("fields")
        if not isinstance(record_id, str) or not isinstance(current_fields, dict):
            continue

        record_type = current_fields.get(FIELD_TYPE)
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

        title = _record_title(current_fields, record_id)
        duration_seconds = _safe_int(current_fields.get(FIELD_DURATION))
        translator = _field_text(current_fields, FIELD_TRANSLATOR) or "(unassigned)"
        editor = _field_text(current_fields, FIELD_EDITOR) or "(unassigned)"

        translation_event = (
            new_status == STATUS_TRANSLATION_DONE
            and not _field_text(current_fields, FIELD_EDITOR)
        )
        editing_event = (
            new_status == STATUS_EDITING_DONE
            and not _has_combined_media(current_fields)
        )
        fast_forward_translation = (
            old_status == STATUS_TODO and new_status == STATUS_EDITING_DONE
        )

        if translation_event or fast_forward_translation:
            events.append(
                StatusWorkEvent(
                    record_id=record_id,
                    record_title=title,
                    record_type=str(record_type),
                    duration_seconds=duration_seconds,
                    kind="translator",
                    participant_name=translator,
                    from_status=old_status,
                    to_status=new_status,
                    detected_at=detected_at_text,
                )
            )

        if editing_event:
            events.append(
                StatusWorkEvent(
                    record_id=record_id,
                    record_title=title,
                    record_type=str(record_type),
                    duration_seconds=duration_seconds,
                    kind="editor",
                    participant_name=editor,
                    from_status=old_status,
                    to_status=new_status,
                    detected_at=detected_at_text,
                )
            )

    return events


def load_status_history(path: Path) -> list[StatusWorkEvent]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    events: list[StatusWorkEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            events.append(StatusWorkEvent(**item))
        except TypeError:
            continue
    return events


def append_status_history(
    path: Path,
    events: list[StatusWorkEvent],
) -> int:
    if not events:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    history = load_status_history(path)
    history.extend(events)
    encoded = json.dumps([asdict(event) for event in history], ensure_ascii=False, indent=2)
    path.write_text(encoded, encoding="utf-8")
    return len(events)


def record_status_history_from_snapshots(
    *,
    project_root: Path,
    previous_records: list[dict[str, Any]] | None,
    current_records: list[dict[str, Any]],
    detected_at: datetime,
    history_path: Path | None = None,
) -> list[StatusWorkEvent]:
    if not previous_records:
        return []
    events = detect_status_work_events(
        previous_records,
        current_records,
        detected_at=detected_at,
    )
    target = project_root / (history_path or DEFAULT_HISTORY_PATH)
    if events:
        append_status_history(target, events)
    return events
