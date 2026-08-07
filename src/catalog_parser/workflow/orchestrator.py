from __future__ import annotations

from pathlib import Path
from typing import Any

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_STATUS,
    FIELD_TYPE,
    WORKFLOW_STATUSES,
)
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_combine import DriveCombineError, verify_drive_output_folder_access
from catalog_parser.workflow.actions import ActionResult, execute_action
from catalog_parser.workflow.config import load_workflow_config
from catalog_parser.workflow.rules import (
    WorkflowActionType,
    is_workflow_type,
    plan_ingest_actions,
    plan_record_actions,
    resolve_assign_editor_actions,
    resolve_assign_timing_editor_actions,
)
from catalog_parser.workflow.approved_thumbnails import (
    process_approved_review_thumbnails_in_workflow,
)
from catalog_parser.workflow.editing_done_thumbnails import (
    notify_editing_done_missing_prepared_thumbnails,
)
from catalog_parser.workflow.table_cache import TableCache, DEFAULT_BACKUP_DIR
from catalog_parser.workflow.publish_schedule import schedule_tomorrow_publish
from catalog_parser.workflow.status_validation import (
    apply_status_reverts,
    detect_invalid_status_transitions,
)


def _record_in_workflow_statuses(record: dict[str, Any]) -> bool:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return False
    return fields.get(FIELD_STATUS) in WORKFLOW_STATUSES


def run_workflow(
    *,
    project_root: Path,
    credentials_path: Path,
    token_path: Path,
    dry_run: bool = False,
    use_console: bool = False,
) -> int:
    config = load_workflow_config(project_root)

    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )

    table_cache = TableCache.load(airtable, project_root=project_root)
    _enforce_status_transition_requirements(
        airtable=airtable,
        table_cache=table_cache,
        project_root=project_root,
        dry_run=dry_run,
    )
    workflow_records = [
        record
        for record in table_cache.filter_records(_record_in_workflow_statuses)
        if is_workflow_type(record.get("fields", {}).get(FIELD_TYPE))
    ]
    print(f"Loaded {len(workflow_records)} workflow record(s)")

    drive_service = get_drive_service(
        credentials_path,
        token_path,
        use_console=use_console,
    )
    docs_service = get_docs_service(
        credentials_path,
        token_path,
        use_console=use_console,
    )

    editing_done_thumb_result = notify_editing_done_missing_prepared_thumbnails(
        project_root=project_root,
        current_records=table_cache.records,
        drive_service=drive_service,
        dry_run=dry_run,
        log=print,
        docs_service=docs_service,
    )
    print(f"Editing-done thumbnails: {editing_done_thumb_result.message}")

    planned_actions = []
    for record in workflow_records:
        planned_actions.extend(plan_record_actions(record))

    translator_slots = [
        (profile.name, profile.weekly_capacity_reels, profile.preferred_translation_type)
        for profile in config.translators
    ]
    editor_slots = [
        (profile.name, profile.weekly_capacity_reels, profile.preferred_editing_type)
        for profile in config.editors
    ]
    preferred_editors_by_translator = {
        profile.name: profile.preferred_editor
        for profile in config.translators
        if profile.preferred_editor
    }
    timing_editor_slots = [
        (profile.name, profile.weekly_capacity_reels, profile.preferred_timing_type)
        for profile in config.timing_editors
    ]
    editor_names = frozenset(profile.name for profile in config.editors)
    # Snapshot for ingest planning: stamp same-run editor picks so dual-role
    # translators are blocked from new translation work in this run.
    ingest_records = [
        {
            "id": record.get("id"),
            "fields": dict(fields) if isinstance((fields := record.get("fields")), dict) else {},
        }
        for record in workflow_records
    ]
    planned_actions = resolve_assign_editor_actions(
        ingest_records,
        planned_actions,
        editors=editor_slots,
        preferred_editors_by_translator=preferred_editors_by_translator,
    )
    planned_actions = resolve_assign_timing_editor_actions(
        ingest_records,
        planned_actions,
        timing_editors=timing_editor_slots,
    )
    planned_actions.extend(
        plan_ingest_actions(
            ingest_records,
            translators=translator_slots,
            target_reel_to_video_ratio=config.target_reel_to_video_ratio,
            max_video_seconds=config.max_video_seconds,
            editor_names=editor_names,
        )
    )

    if not planned_actions:
        print("No workflow actions planned.")
    else:
        if not dry_run and any(
            action.action_type == WorkflowActionType.COMBINE_MEDIA
            for action in planned_actions
        ):
            output_parent_id = extract_drive_folder_id(config.output_drive_folder)
            if output_parent_id is None:
                print(
                    "ERROR: Could not parse output Drive folder id from "
                    f"{config.output_drive_folder!r}"
                )
                return 1
            try:
                verify_drive_output_folder_access(drive_service, output_parent_id)
            except DriveCombineError as exc:
                print(f"ERROR: {exc}")
                return 1

        print(f"Planned {len(planned_actions)} action(s){' (dry-run)' if dry_run else ''}:")
        results: list[ActionResult] = []
        for action in planned_actions:
            label = action.title or action.translator_name or action.record_id
            print(f"  - {action.action_type.value}: {label} ({action.reason})")
            result = execute_action(
                action,
                airtable=airtable,
                config=config,
                drive_service=drive_service,
                docs_service=docs_service,
                credentials_path=credentials_path,
                token_path=token_path,
                dry_run=dry_run,
                use_console=use_console,
                table_cache=table_cache,
                project_root=project_root,
            )
            results.append(result)
            status = "OK" if result.success else "FAIL"
            print(f"    -> {status}: {result.message}")

        failures = sum(1 for result in results if not result.success)
        if failures:
            return 1

    approved_result = process_approved_review_thumbnails_in_workflow(
        project_root=project_root,
        records=table_cache.records,
        dry_run=dry_run,
        log=print,
    )

    schedule_result = schedule_tomorrow_publish(
        airtable=airtable,
        records=table_cache.records,
        drive_service=drive_service,
        dry_run=dry_run,
        log=print,
        project_root=project_root,
        docs_service=docs_service,
    )
    print(f"Publish schedule: {schedule_result.message}")
    if not schedule_result.success:
        return 1
    if approved_result.processed:
        print(f"Approved thumbnails: {approved_result.processed} file(s) handled")
    return 0


def _require_env(name: str) -> str:
    import os

    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _enforce_status_transition_requirements(
    *,
    airtable: AirtableClient,
    table_cache: TableCache,
    project_root: Path,
    dry_run: bool,
) -> None:
    previous_path = project_root / DEFAULT_BACKUP_DIR / "airtable-previous.json"
    if not previous_path.is_file():
        return

    try:
        previous_records = TableCache.from_backup_file(previous_path).records
    except ValueError as exc:
        print(f"Warning: could not load previous backup for status validation: {exc}")
        return

    actions = detect_invalid_status_transitions(
        previous_records,
        table_cache.records,
    )
    apply_status_reverts(
        airtable=airtable,
        table_cache=table_cache,
        actions=actions,
        dry_run=dry_run,
    )
