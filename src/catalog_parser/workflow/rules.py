from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_STATUS,
    FIELD_DURATION,
    FIELD_TRANSLATED_SUBTITLES,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TRANSLATOR,
    FIELD_EDITOR,
    FIELD_TIMING_EDITOR,
    FIELD_TITLE,
    FIELD_TYPE,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO

VIDEO_REEL_EQUIVALENT = 10


class WorkflowActionType(str, Enum):
    COMBINE_MEDIA = "combine_media"
    INGEST_FOR_TRANSLATOR = "ingest_for_translator"
    INGEST_FOR_EDITOR = "ingest_for_editor"
    ASSIGN_EDITOR = "assign_editor"
    ASSIGN_TIMING_EDITOR = "assign_timing_editor"


@dataclass(frozen=True)
class WorkflowAction:
    action_type: WorkflowActionType
    record_id: str | None = None
    title: str | None = None
    target_status: str | None = None
    translator_name: str | None = None
    editor_name: str | None = None
    timing_editor_name: str | None = None
    ingest_type: str | None = None
    ingest_count: int | None = None
    reason: str = ""


WORKFLOW_TYPES = (TYPE_REEL, TYPE_VIDEO)


def is_workflow_type(record_type: Any) -> bool:
    return record_type in WORKFLOW_TYPES


def _has_combined_media(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        value = str(value)
    return bool(value.strip())


def _has_translated_subtitles(value: Any) -> bool:
    return _has_combined_media(value)


def plan_record_actions(
    record: dict[str, Any],
) -> list[WorkflowAction]:
    record_id = record.get("id")
    if not isinstance(record_id, str):
        return []

    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return []

    title = fields.get(FIELD_TITLE)
    status = fields.get(FIELD_STATUS)
    record_type = fields.get("Type")
    combined_media = fields.get(FIELD_COMBINED_MEDIA_FILE)
    translated_subtitles = fields.get(FIELD_TRANSLATED_SUBTITLES)
    translation_resources = fields.get(FIELD_TRANSLATION_RESOURCES)
    timing_editor = fields.get(FIELD_TIMING_EDITOR)

    if not is_workflow_type(record_type):
        return []

    actions: list[WorkflowAction] = []
    title_text = title if isinstance(title, str) else record_id

    if status == STATUS_EDITING_DONE and not timing_editor:
        actions.append(
            WorkflowAction(
                action_type=WorkflowActionType.ASSIGN_TIMING_EDITOR,
                record_id=record_id,
                title=title_text,
                reason="Editing done; needs timing editor assignment",
            )
        )

    needs_subtitles = _has_combined_media(
        translation_resources
    ) and not _has_translated_subtitles(translated_subtitles)
    if status == STATUS_EDITING_DONE and (
        not _has_combined_media(combined_media) or needs_subtitles
    ):
        if not _has_combined_media(combined_media):
            reason = "Editing done; combined media missing"
        else:
            reason = "Editing done; aligned subtitles missing"
        actions.append(
            WorkflowAction(
                action_type=WorkflowActionType.COMBINE_MEDIA,
                record_id=record_id,
                title=title_text,
                reason=reason,
            )
        )

    return actions


def count_active_assignments(
    records: list[dict[str, Any]],
    translator_name: str,
) -> int:
    active = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_TRANSLATOR) != translator_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_TODO:
            active += 1
    return active


def _record_duration_seconds(fields: dict[str, Any]) -> int:
    duration = fields.get(FIELD_DURATION)
    if isinstance(duration, int):
        return max(0, duration)
    try:
        return max(0, int(duration))
    except Exception:
        return 0


def count_active_translation_seconds(
    records: list[dict[str, Any]],
    translator_name: str,
) -> int:
    total = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_TRANSLATOR) != translator_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_TODO:
            total += _record_duration_seconds(fields)
    return total


def record_reel_units(fields: dict[str, Any]) -> int:
    record_type = fields.get("Type")
    if record_type == TYPE_VIDEO:
        return VIDEO_REEL_EQUIVALENT
    if record_type == TYPE_REEL:
        return 1
    return 0


def count_active_translation_reel_units(
    records: list[dict[str, Any]],
    translator_name: str,
) -> int:
    total = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_TRANSLATOR) != translator_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_TODO:
            total += record_reel_units(fields)
    return total


def count_active_editing_seconds(
    records: list[dict[str, Any]],
    editor_name: str,
) -> int:
    total = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_EDITOR) != editor_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_TRANSLATION_DONE:
            total += _record_duration_seconds(fields)
    return total


def count_active_editing_reel_units(
    records: list[dict[str, Any]],
    editor_name: str,
) -> int:
    total = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_EDITOR) != editor_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_TRANSLATION_DONE:
            total += record_reel_units(fields)
    return total


def choose_editor(
    records: list[dict[str, Any]],
    *,
    record_type: Any,
    editors: list[tuple[str, int, str | None]],
    preferred_editor: str | None = None,
) -> str | None:
    """Pick an editor for ``record_type``.

    Each editor is ``(name, weekly_capacity_reels, preferred_editing_type)``.
    When ``preferred_editor`` is set, that person is chosen (type preference ignored).
    Otherwise pick the least-utilized eligible editor by type preference.
    """
    if preferred_editor:
        return preferred_editor

    eligible: list[tuple[str, int]] = []
    for name, capacity, preferred in editors:
        if preferred and preferred != record_type:
            continue
        eligible.append((name, capacity))
    if not eligible:
        return None

    def utilization(item: tuple[str, int]) -> float:
        name, capacity = item
        return count_active_editing_reel_units(records, name) / max(1, capacity)

    return sorted(eligible, key=utilization)[0][0]


def resolve_assign_editor_actions(
    records: list[dict[str, Any]],
    actions: list[WorkflowAction],
    *,
    editors: list[tuple[str, int, str | None]],
    preferred_editors_by_translator: dict[str, str] | None = None,
) -> list[WorkflowAction]:
    """Choose editors for assign_editor actions and stamp them onto ``records``.

    Mutates ``records`` so same-run translation ingest sees the new editing load.
    Preferred-editor assignments (from translator config) are resolved first so they
    claim capacity/utilization before general least-utilized picks.
    """
    by_id = {
        record_id: record
        for record in records
        if isinstance((record_id := record.get("id")), str)
    }
    preferred_map = preferred_editors_by_translator or {}

    def translator_preferred_editor(action: WorkflowAction) -> str | None:
        if not action.record_id:
            return None
        record = by_id.get(action.record_id)
        if record is None:
            return None
        fields = record.get("fields")
        if not isinstance(fields, dict):
            return None
        translator = fields.get(FIELD_TRANSLATOR)
        if not isinstance(translator, str) or not translator.strip():
            return None
        return preferred_map.get(translator.strip())

    editor_actions = [
        action for action in actions if action.action_type == WorkflowActionType.ASSIGN_EDITOR
    ]
    other_actions = [
        action for action in actions if action.action_type != WorkflowActionType.ASSIGN_EDITOR
    ]
    preferred_actions = [
        action for action in editor_actions if translator_preferred_editor(action)
    ]
    general_actions = [
        action for action in editor_actions if not translator_preferred_editor(action)
    ]

    resolved_editors: list[WorkflowAction] = []
    for action in preferred_actions + general_actions:
        if not action.record_id:
            resolved_editors.append(action)
            continue
        record = by_id.get(action.record_id)
        if record is None:
            resolved_editors.append(action)
            continue
        fields = record.get("fields")
        if not isinstance(fields, dict):
            resolved_editors.append(action)
            continue
        preferred = action.editor_name or translator_preferred_editor(action)
        chosen = preferred or choose_editor(
            records,
            record_type=fields.get("Type"),
            editors=editors,
            preferred_editor=None,
        )
        if chosen is None:
            resolved_editors.append(action)
            continue
        fields[FIELD_EDITOR] = chosen
        reason = action.reason
        if preferred and preferred == chosen and not action.editor_name:
            reason = f"{reason}; preferred editor {chosen}"
        else:
            reason = f"{reason}; assign {chosen}"
        resolved_editors.append(
            WorkflowAction(
                action_type=action.action_type,
                record_id=action.record_id,
                title=action.title,
                editor_name=chosen,
                reason=reason,
            )
        )

    # Preferred editor work runs first in the action list for this run.
    return resolved_editors + other_actions


def count_active_timing_reel_units(
    records: list[dict[str, Any]],
    timing_editor_name: str,
) -> int:
    total = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_TIMING_EDITOR) != timing_editor_name:
            continue
        status = fields.get(FIELD_STATUS)
        if status == STATUS_EDITING_DONE:
            total += record_reel_units(fields)
    return total


def choose_timing_editor(
    records: list[dict[str, Any]],
    *,
    record_type: Any,
    timing_editors: list[tuple[str, int, str | None]],
) -> str | None:
    """Pick the least-utilized eligible timing editor for ``record_type``.

    Each timing editor is ``(name, weekly_capacity_reels, preferred_timing_type)``.
    Skips people whose preference does not match ``record_type``. Capacity is used
    only for utilization ranking (same as editor assignment); it is not a hard cap.
    """
    eligible: list[tuple[str, int]] = []
    for name, capacity, preferred in timing_editors:
        if preferred and preferred != record_type:
            continue
        eligible.append((name, capacity))
    if not eligible:
        return None

    def utilization(item: tuple[str, int]) -> float:
        name, capacity = item
        return count_active_timing_reel_units(records, name) / max(1, capacity)

    return sorted(eligible, key=utilization)[0][0]


def resolve_assign_timing_editor_actions(
    records: list[dict[str, Any]],
    actions: list[WorkflowAction],
    *,
    timing_editors: list[tuple[str, int, str | None]],
) -> list[WorkflowAction]:
    """Choose timing editors for assign_timing_editor actions and stamp them onto ``records``.

    Mutates ``records`` so utilization updates between assignments in the same run.
    Videos are resolved before Reels so flexible timing editors are claimed by
    waiting Videos first (capacity is utilization weight only, not a hard cap).
    """
    by_id = {
        record_id: record
        for record in records
        if isinstance((record_id := record.get("id")), str)
    }

    def record_type_for_action(action: WorkflowAction) -> Any:
        if not action.record_id:
            return None
        record = by_id.get(action.record_id)
        if record is None:
            return None
        fields = record.get("fields")
        if not isinstance(fields, dict):
            return None
        return fields.get("Type")

    def timing_sort_key(action: WorkflowAction) -> tuple[int, str]:
        record_type = record_type_for_action(action)
        if record_type == TYPE_VIDEO:
            return (0, action.record_id or "")
        if record_type == TYPE_REEL:
            return (1, action.record_id or "")
        return (2, action.record_id or "")

    timing_actions = [
        action
        for action in actions
        if action.action_type == WorkflowActionType.ASSIGN_TIMING_EDITOR
    ]
    timing_actions = sorted(timing_actions, key=timing_sort_key)

    resolved_timing: list[WorkflowAction] = []
    for action in timing_actions:
        if not action.record_id:
            resolved_timing.append(action)
            continue
        record = by_id.get(action.record_id)
        if record is None:
            resolved_timing.append(action)
            continue
        fields = record.get("fields")
        if not isinstance(fields, dict):
            resolved_timing.append(action)
            continue
        record_type = fields.get("Type")
        chosen = action.timing_editor_name or choose_timing_editor(
            records,
            record_type=record_type,
            timing_editors=timing_editors,
        )
        if chosen is None:
            resolved_timing.append(action)
            continue
        fields[FIELD_TIMING_EDITOR] = chosen
        resolved_timing.append(
            WorkflowAction(
                action_type=action.action_type,
                record_id=action.record_id,
                title=action.title,
                timing_editor_name=chosen,
                reason=f"{action.reason}; assign {chosen}",
            )
        )

    timing_queue = list(resolved_timing)
    resolved: list[WorkflowAction] = []
    for action in actions:
        if action.action_type == WorkflowActionType.ASSIGN_TIMING_EDITOR:
            resolved.append(timing_queue.pop(0))
        else:
            resolved.append(action)
    return resolved


def pool_type_counts(records: list[dict[str, Any]]) -> tuple[int, int]:
    reels = 0
    videos = 0
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        record_type = fields.get("Type")
        if record_type == TYPE_REEL:
            reels += 1
        elif record_type == TYPE_VIDEO:
            videos += 1
    return reels, videos


def choose_ingest_type(
    *,
    preferred: str | None,
    reels_pool: int,
    videos_pool: int,
    target_reel_to_video_ratio: int,
) -> str:
    if preferred in {TYPE_REEL, TYPE_VIDEO}:
        return preferred
    if videos_pool <= 0:
        return TYPE_VIDEO
    current_ratio = reels_pool / max(1, videos_pool)
    return TYPE_VIDEO if current_ratio >= target_reel_to_video_ratio else TYPE_REEL


def plan_capacity_ingest_chunks(
    *,
    weekly_capacity_reels: int,
    preferred_type: str | None,
    active_units: int,
    reels_pool: int,
    videos_pool: int,
    target_reel_to_video_ratio: int,
) -> tuple[list[tuple[str, int]], int, int]:
    """Fill remaining weekly capacity with ingest chunks.

    Returns ``(chunks, reels_pool, videos_pool)`` where each chunk is
    ``(ingest_type, ingest_count)``.
    """
    chunks: list[tuple[str, int]] = []
    while active_units < weekly_capacity_reels:
        remaining_capacity_units = weekly_capacity_reels - active_units
        ingest_type = choose_ingest_type(
            preferred=preferred_type,
            reels_pool=reels_pool,
            videos_pool=videos_pool,
            target_reel_to_video_ratio=target_reel_to_video_ratio,
        )
        if ingest_type == TYPE_VIDEO and remaining_capacity_units < VIDEO_REEL_EQUIVALENT:
            if preferred_type in {TYPE_VIDEO, TYPE_REEL}:
                break
            if weekly_capacity_reels >= VIDEO_REEL_EQUIVALENT:
                # Ratio wants a video and this person can hold one eventually.
                # Do not consume the remaining capacity with reels while waiting.
                break
            ingest_type = TYPE_REEL
        if ingest_type == TYPE_REEL and remaining_capacity_units < 1:
            break

        if ingest_type == TYPE_VIDEO:
            ingest_units = VIDEO_REEL_EQUIVALENT
            ingest_count = 1
        else:
            ingest_units = remaining_capacity_units
            ingest_count = ingest_units

        if active_units + ingest_units > weekly_capacity_reels:
            break

        chunks.append((ingest_type, ingest_count))
        if ingest_type == TYPE_VIDEO:
            videos_pool += 1
        else:
            reels_pool += ingest_count
        active_units += ingest_units
    return chunks, reels_pool, videos_pool


def editor_due_for_weekly_assignment(
    *,
    last_assigned: date | None,
    today: date,
    assignment_weekday: int | None = None,
) -> bool:
    """True until this week's assignment weekday fill has been recorded.

    Default weekday is Monday. Tuesday–Sunday catch up if last week's date is
    still stored (Monday run missed or fill failed).
    """
    from catalog_parser.workflow.editor_idle import (
        EDITOR_ASSIGNMENT_WEEKDAY,
        this_week_assignment_date,
    )

    weekday = (
        EDITOR_ASSIGNMENT_WEEKDAY if assignment_weekday is None else assignment_weekday
    )
    week_date = this_week_assignment_date(today, weekday=weekday)
    if last_assigned is None:
        return True
    return last_assigned < week_date


def _unassigned_translation_done_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    unassigned: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if fields.get(FIELD_STATUS) != STATUS_TRANSLATION_DONE:
            continue
        editor = fields.get(FIELD_EDITOR)
        if isinstance(editor, str) and editor.strip():
            continue
        if not is_workflow_type(fields.get(FIELD_TYPE)):
            continue
        unassigned.append(record)
    unassigned.sort(
        key=lambda rec: (str(rec.get("createdTime") or ""), str(rec.get("id") or ""))
    )
    return unassigned


def _translator_preferred_editor(
    fields: dict[str, Any],
    preferred_editors_by_translator: dict[str, str],
) -> str | None:
    translator = fields.get(FIELD_TRANSLATOR)
    if not isinstance(translator, str) or not translator.strip():
        return None
    return preferred_editors_by_translator.get(translator.strip())


def _claim_unassigned_for_editor(
    unassigned: list[dict[str, Any]],
    *,
    editor_name: str,
    preferred_type: str | None,
    remaining_units: int,
    preferred_editors_by_translator: dict[str, str],
    editor_names: frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    claimed: list[dict[str, Any]] = []
    leftover: list[dict[str, Any]] = []
    remaining = remaining_units

    def try_claim(record: dict[str, Any], *, ignore_type: bool) -> None:
        nonlocal remaining
        fields = record.get("fields")
        if not isinstance(fields, dict):
            leftover.append(record)
            return
        units = record_reel_units(fields)
        record_type = fields.get(FIELD_TYPE)
        if units <= 0 or units > remaining:
            leftover.append(record)
            return
        if not ignore_type and preferred_type and record_type != preferred_type:
            leftover.append(record)
            return
        claimed.append(record)
        remaining -= units

    reserved_for_self: list[dict[str, Any]] = []
    general: list[dict[str, Any]] = []
    for record in unassigned:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            leftover.append(record)
            continue
        preferred = _translator_preferred_editor(
            fields, preferred_editors_by_translator
        )
        if preferred == editor_name:
            reserved_for_self.append(record)
        elif preferred and preferred in editor_names:
            leftover.append(record)
        else:
            general.append(record)

    for record in reserved_for_self:
        if remaining <= 0:
            leftover.append(record)
            continue
        try_claim(record, ignore_type=True)
    for record in general:
        if remaining <= 0:
            leftover.append(record)
            continue
        try_claim(record, ignore_type=False)
    return claimed, leftover, remaining


def stamp_pending_editor_ingest(
    records: list[dict[str, Any]],
    action: WorkflowAction,
) -> None:
    """Append placeholder rows so later ingest planning sees the new editing load."""
    from catalog_parser.workflow.editor_idle import SIR_TRANSLATESALOT

    count = action.ingest_count or 0
    ingest_type = action.ingest_type
    editor_name = action.editor_name
    if count <= 0 or not ingest_type or not editor_name:
        return
    for index in range(count):
        records.append(
            {
                "id": f"pending-idle-{editor_name}-{ingest_type}-{index}",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_EDITOR: editor_name,
                    FIELD_TYPE: ingest_type,
                    FIELD_TRANSLATOR: SIR_TRANSLATESALOT,
                },
            }
        )


def plan_weekly_editor_assignment_actions(
    records: list[dict[str, Any]],
    *,
    editors: list[tuple[str, int, str | None]],
    last_assigned: dict[str, date],
    today: date,
    target_reel_to_video_ratio: int,
    preferred_editors_by_translator: dict[str, str] | None = None,
) -> tuple[list[WorkflowAction], list[str]]:
    """Assign each due editor a full week of work from unassigned Translation done.

    Already-assigned Translation done does not reduce this week's batch. Shortfall
    (or the wrong type) is ingested as Translation done with translator Sir
    Translatesalot. Returns actions plus editor names whose weekly pass was
    attempted (mark last-assigned only if those actions succeed).
    """
    from catalog_parser.workflow.editor_idle import SIR_TRANSLATESALOT

    preferred_map = preferred_editors_by_translator or {}
    editor_names = frozenset(name for name, _capacity, _preferred in editors)
    actions: list[WorkflowAction] = []
    processed: list[str] = []
    unassigned = _unassigned_translation_done_records(records)
    reels_pool, videos_pool = pool_type_counts(records)

    for editor_name, weekly_capacity_reels, preferred_type in editors:
        if not editor_due_for_weekly_assignment(
            last_assigned=last_assigned.get(editor_name),
            today=today,
        ):
            continue
        processed.append(editor_name)
        remaining = weekly_capacity_reels
        claimed, unassigned, remaining = _claim_unassigned_for_editor(
            unassigned,
            editor_name=editor_name,
            preferred_type=preferred_type,
            remaining_units=remaining,
            preferred_editors_by_translator=preferred_map,
            editor_names=editor_names,
        )
        for record in claimed:
            fields = record.get("fields")
            if not isinstance(fields, dict):
                continue
            fields[FIELD_EDITOR] = editor_name
            record_id = record.get("id")
            title = fields.get(FIELD_TITLE)
            title_text = title if isinstance(title, str) else str(record_id)
            units = record_reel_units(fields)
            actions.append(
                WorkflowAction(
                    action_type=WorkflowActionType.ASSIGN_EDITOR,
                    record_id=record_id if isinstance(record_id, str) else None,
                    title=title_text,
                    editor_name=editor_name,
                    reason=(
                        f"weekly editor fill {editor_name}: assign {title_text} "
                        f"({units}u)"
                    ),
                )
            )

        chunks, reels_pool, videos_pool = plan_capacity_ingest_chunks(
            weekly_capacity_reels=weekly_capacity_reels,
            preferred_type=preferred_type,
            active_units=weekly_capacity_reels - remaining,
            reels_pool=reels_pool,
            videos_pool=videos_pool,
            target_reel_to_video_ratio=target_reel_to_video_ratio,
        )
        for ingest_type, ingest_count in chunks:
            action = WorkflowAction(
                action_type=WorkflowActionType.INGEST_FOR_EDITOR,
                translator_name=SIR_TRANSLATESALOT,
                editor_name=editor_name,
                ingest_type=ingest_type,
                ingest_count=ingest_count,
                target_status=STATUS_TRANSLATION_DONE,
                reason=(
                    f"weekly editor fill {editor_name}: "
                    f"{weekly_capacity_reels - remaining}/{weekly_capacity_reels} units; "
                    f"ingest {ingest_count} {ingest_type}(s) as Translation done"
                ),
            )
            actions.append(action)
            stamp_pending_editor_ingest(records, action)
            if ingest_type == TYPE_VIDEO:
                remaining = max(0, remaining - VIDEO_REEL_EQUIVALENT)
            else:
                remaining = max(0, remaining - ingest_count)

    return actions, processed


def plan_ingest_actions(
    records: list[dict[str, Any]],
    *,
    translators: list[tuple[str, int, str | None]],
    target_reel_to_video_ratio: int,
    max_video_seconds: int,
    editor_names: frozenset[str] | None = None,
) -> list[WorkflowAction]:
    actions: list[WorkflowAction] = []
    reels_pool, videos_pool = pool_type_counts(records)
    editors = editor_names or frozenset()

    for translator_name, weekly_capacity_reels, preferred_type in translators:
        if (
            translator_name in editors
            and count_active_editing_reel_units(records, translator_name) > 0
        ):
            continue

        active_units = count_active_translation_reel_units(records, translator_name)
        chunks, reels_pool, videos_pool = plan_capacity_ingest_chunks(
            weekly_capacity_reels=weekly_capacity_reels,
            preferred_type=preferred_type,
            active_units=active_units,
            reels_pool=reels_pool,
            videos_pool=videos_pool,
            target_reel_to_video_ratio=target_reel_to_video_ratio,
        )
        for ingest_type, ingest_count in chunks:
            actions.append(
                WorkflowAction(
                    action_type=WorkflowActionType.INGEST_FOR_TRANSLATOR,
                    translator_name=translator_name,
                    ingest_type=ingest_type,
                    ingest_count=ingest_count,
                    reason=(
                        f"translator units {active_units}/{weekly_capacity_reels}; "
                        f"assign {ingest_count} {ingest_type}(s)"
                    ),
                )
            )
            if ingest_type == TYPE_VIDEO:
                active_units += VIDEO_REEL_EQUIVALENT
            else:
                active_units += ingest_count
    return actions
