from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from googleapiclient.discovery import Resource

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_EDITOR,
    FIELD_TITLE,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import (
    check_mixable_media,
    format_mix_media_check,
    mix_folder_media_to_drive,
)
from catalog_parser.workflow.config import WorkflowConfig
from catalog_parser.workflow.ingest import ingest_batch_for_translator
from catalog_parser.workflow.rules import (
    WorkflowAction,
    WorkflowActionType,
    choose_editor,
)
from catalog_parser.workflow.table_cache import TableCache


@dataclass
class ActionResult:
    action: WorkflowAction
    success: bool
    message: str


def execute_action(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    drive_service: Resource | None,
    docs_service: Resource | None,
    credentials_path: Path,
    token_path: Path,
    dry_run: bool,
    use_console: bool = False,
    table_cache: TableCache | None = None,
) -> ActionResult:
    if action.action_type == WorkflowActionType.COMBINE_MEDIA:
        return _combine_media(
            action,
            airtable=airtable,
            config=config,
            dry_run=dry_run,
            table_cache=table_cache,
        )
    if action.action_type == WorkflowActionType.INGEST_FOR_TRANSLATOR:
        return _ingest_for_translator(
            action,
            airtable=airtable,
            config=config,
            credentials_path=credentials_path,
            token_path=token_path,
            dry_run=dry_run,
            use_console=use_console,
            table_cache=table_cache,
        )
    if action.action_type == WorkflowActionType.ASSIGN_EDITOR:
        return _assign_editor(
            action,
            airtable=airtable,
            config=config,
            table_cache=table_cache,
            dry_run=dry_run,
        )
    return ActionResult(action=action, success=False, message=f"Unknown action: {action.action_type}")


def _combine_media(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    dry_run: bool,
    table_cache: TableCache | None = None,
) -> ActionResult:
    if not action.record_id:
        return ActionResult(action=action, success=False, message="Missing record_id")

    record = table_cache.get(action.record_id) if table_cache is not None else None
    if record is None:
        record = airtable.get_record(action.record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return ActionResult(action=action, success=False, message="Record has no fields")

    drive_link = fields.get("Video Folder")
    title = fields.get(FIELD_TITLE)
    if not isinstance(drive_link, str) or not drive_link.strip():
        return ActionResult(action=action, success=False, message="Missing Video Folder")
    if not isinstance(title, str) or not title.strip():
        return ActionResult(action=action, success=False, message=f"Missing {FIELD_TITLE}")

    pkg_folder_id = extract_drive_folder_id(drive_link)
    output_parent_id = extract_drive_folder_id(config.output_drive_folder)
    if pkg_folder_id is None or output_parent_id is None:
        return ActionResult(action=action, success=False, message="Could not parse Drive folder id")

    output_name = title if title.casefold().endswith(".mp4") else f"{title}.mp4"
    if dry_run:
        drive = get_drive_service_noninteractive()
        check = check_mixable_media(drive, pkg_folder_id)
        if not check.ok:
            return ActionResult(
                action=action,
                success=False,
                message=format_mix_media_check(check),
            )
        return ActionResult(
            action=action,
            success=True,
            message=f"Would combine media -> {output_name}; {format_mix_media_check(check)}",
        )

    drive = get_drive_service_noninteractive()
    work_dir = config.work_dir / action.record_id
    created = mix_folder_media_to_drive(
        drive,
        pkg_folder_id=pkg_folder_id,
        output_parent_id=output_parent_id,
        output_name=output_name,
        work_dir=work_dir,
        dry_run=False,
    )
    drive_url = f"https://drive.google.com/file/d/{created.id}/view"
    airtable.update_record_fields(
        action.record_id,
        {FIELD_COMBINED_MEDIA_FILE: drive_url},
    )
    if table_cache is not None:
        table_cache.update_fields(
            action.record_id,
            {FIELD_COMBINED_MEDIA_FILE: drive_url},
        )
    return ActionResult(
        action=action,
        success=True,
        message=f"Combined media uploaded: {drive_url} (local: {work_dir / output_name})",
    )


def _ingest_for_translator(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    credentials_path: Path,
    token_path: Path,
    dry_run: bool,
    use_console: bool,
    table_cache: TableCache | None = None,
) -> ActionResult:
    if not action.translator_name:
        return ActionResult(action=action, success=False, message="Missing translator_name")
    if action.ingest_type is None or action.ingest_count is None:
        return ActionResult(action=action, success=False, message="Missing ingest_type/ingest_count")
    if dry_run:
        return ActionResult(
            action=action,
            success=True,
            message=f"Would ingest {action.ingest_count} {action.ingest_type}(s) for translator {action.translator_name!r}",
        )

    created_ids = ingest_batch_for_translator(
        airtable,
        translator_name=action.translator_name,
        desired_type=action.ingest_type,
        target_count=action.ingest_count,
        max_video_seconds=config.max_video_seconds,
        credentials_path=credentials_path,
        token_path=token_path,
        use_console=use_console,
        table_cache=table_cache,
    )
    if not created_ids:
        return ActionResult(
            action=action,
            success=False,
            message=f"No eligible catalog row found for {action.translator_name!r}",
        )

    return ActionResult(
        action=action,
        success=True,
        message=f"Ingested {len(created_ids)} record(s) and assigned to {action.translator_name!r}: {', '.join(created_ids)}",
    )


def _assign_editor(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    table_cache: TableCache | None,
    dry_run: bool,
) -> ActionResult:
    if not action.record_id:
        return ActionResult(action=action, success=False, message="Missing record_id")

    record = table_cache.get(action.record_id) if table_cache is not None else None
    if record is None:
        record = airtable.get_record(action.record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return ActionResult(action=action, success=False, message="Record has no fields")

    if action.editor_name:
        chosen_name = action.editor_name
    else:
        record_type = fields.get("Type")
        editor_slots = [
            (editor.name, editor.weekly_capacity_reels, editor.preferred_editing_type)
            for editor in config.editors
        ]
        records_for_utilization = (
            table_cache.records if table_cache is not None else airtable.list_records()
        )
        chosen_name = choose_editor(
            records_for_utilization,
            record_type=record_type,
            editors=editor_slots,
        )
        if chosen_name is None:
            return ActionResult(
                action=action,
                success=False,
                message="No eligible editors for this type",
            )

    if dry_run:
        return ActionResult(
            action=action,
            success=True,
            message=f"Would assign editor {chosen_name!r}",
        )
    airtable.update_record_fields(action.record_id, {FIELD_EDITOR: chosen_name})
    if table_cache is not None:
        table_cache.update_fields(action.record_id, {FIELD_EDITOR: chosen_name})
    return ActionResult(action=action, success=True, message=f"Assigned editor {chosen_name!r}")
