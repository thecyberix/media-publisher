from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from catalog_parser.airtable import FIELD_EDITOR, FIELD_STATUS, STATUS_TRANSLATION_DONE

DEFAULT_EDITOR_LAST_ASSIGNED_PATH = Path("output") / "workflow" / "editor_last_assigned.json"
DEFAULT_WORKFLOW_TIMEZONE = "Europe/Sofia"
SIR_TRANSLATESALOT = "Sir Translatesalot"
IDLE_EDITOR_GRACE_DAYS = 7


def editor_last_assigned_path(project_root: Path) -> Path:
    return project_root / DEFAULT_EDITOR_LAST_ASSIGNED_PATH


def workflow_today(*, timezone_name: str = DEFAULT_WORKFLOW_TIMEZONE) -> date:
    from media_publisher.timezones import get_timezone

    return datetime.now(get_timezone(timezone_name)).date()


def load_editor_last_assigned(path: Path) -> dict[str, date]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    assigned: dict[str, date] = {}
    for name, raw in payload.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(raw, str):
            continue
        try:
            assigned[name.strip()] = date.fromisoformat(raw[:10])
        except ValueError:
            continue
    return assigned


def save_editor_last_assigned(path: Path, assigned: dict[str, date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        name: assigned_on.isoformat()
        for name, assigned_on in sorted(assigned.items())
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def mark_editor_assigned(
    assigned: dict[str, date],
    editor_name: str,
    *,
    when: date,
) -> None:
    name = editor_name.strip()
    if not name:
        return
    previous = assigned.get(name)
    if previous is None or when > previous:
        assigned[name] = when


def _editor_name(fields: dict[str, Any]) -> str | None:
    value = fields.get(FIELD_EDITOR)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _has_active_editing_queue(records: list[dict[str, Any]], editor_name: str) -> bool:
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if _editor_name(fields) != editor_name:
            continue
        if fields.get(FIELD_STATUS) == STATUS_TRANSLATION_DONE:
            return True
    return False


def seed_editor_last_assigned(
    assigned: dict[str, date],
    *,
    records: list[dict[str, Any]],
    previous_records: list[dict[str, Any]] | None,
    editor_names: Iterable[str],
    today: date,
) -> None:
    """Record new assignments from the current table snapshot.

    Existing records whose Editor field newly appears are treated as assigned today.
    Editors who currently hold Translation done work with no stored date are marked
    assigned today so a soon-empty queue is not treated as "never assigned."
    Idle editors with no stored date are left unknown (eligible to ingest).
    """
    names = {name.strip() for name in editor_names if name.strip()}
    previous_by_id: dict[str, dict[str, Any]] = {}
    if previous_records:
        previous_by_id = {
            record_id: record
            for record in previous_records
            if isinstance((record_id := record.get("id")), str)
        }

    for record in records:
        record_id = record.get("id")
        fields = record.get("fields")
        if not isinstance(record_id, str) or not isinstance(fields, dict):
            continue
        editor = _editor_name(fields)
        if editor is None or editor not in names:
            continue
        previous = previous_by_id.get(record_id)
        if previous is None:
            continue
        previous_fields = previous.get("fields")
        if not isinstance(previous_fields, dict):
            previous_fields = {}
        previous_editor = _editor_name(previous_fields)
        if previous_editor != editor:
            mark_editor_assigned(assigned, editor, when=today)

    for name in names:
        if name in assigned:
            continue
        if _has_active_editing_queue(records, name):
            assigned[name] = today
