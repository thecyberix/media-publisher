from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from googleapiclient.discovery import Resource

from catalog_parser.airtable import (
    AirtableClient,
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_EDITOR,
    FIELD_TIMING_EDITOR,
    FIELD_TITLE,
    FIELD_TRANSLATED_SUBTITLES,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import (
    check_mixable_media,
    format_mix_media_check,
    mix_folder_media_to_drive,
    pick_dialogue_audio_path,
)
from catalog_parser.workflow.config import WorkflowConfig, combined_media_output_folder_id
from catalog_parser.workflow.ingest import ingest_batch_for_translator
from catalog_parser.workflow.rules import (
    WorkflowAction,
    WorkflowActionType,
    choose_editor,
    choose_timing_editor,
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
    project_root: Path | None = None,
) -> ActionResult:
    if action.action_type == WorkflowActionType.COMBINE_MEDIA:
        return _combine_media(
            action,
            airtable=airtable,
            config=config,
            dry_run=dry_run,
            table_cache=table_cache,
            project_root=project_root,
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
    if action.action_type == WorkflowActionType.ASSIGN_TIMING_EDITOR:
        return _assign_timing_editor(
            action,
            airtable=airtable,
            config=config,
            table_cache=table_cache,
            dry_run=dry_run,
            drive_service=drive_service,
            docs_service=docs_service,
            project_root=project_root,
        )
    return ActionResult(action=action, success=False, message=f"Unknown action: {action.action_type}")


def _combine_media(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    dry_run: bool,
    table_cache: TableCache | None = None,
    project_root: Path | None = None,
) -> ActionResult:
    if not action.record_id:
        return ActionResult(action=action, success=False, message="Missing record_id")

    record = table_cache.get(action.record_id) if table_cache is not None else None
    if record is None:
        record = airtable.get_record(action.record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return ActionResult(action=action, success=False, message="Record has no fields")

    drive_link = fields.get(FIELD_VIDEO_FOLDER)
    title = fields.get(FIELD_TITLE)
    if not isinstance(drive_link, str) or not drive_link.strip():
        return ActionResult(action=action, success=False, message="Missing Video Folder")
    if not isinstance(title, str) or not title.strip():
        return ActionResult(action=action, success=False, message=f"Missing {FIELD_TITLE}")

    pkg_folder_id = extract_drive_folder_id(drive_link)
    drive = get_drive_service_noninteractive()
    try:
        output_parent_id = combined_media_output_folder_id(config, drive)
    except Exception as exc:
        return ActionResult(
            action=action,
            success=False,
            message=f"Could not resolve Combined Media Files folder: {exc}",
        )
    if pkg_folder_id is None:
        return ActionResult(action=action, success=False, message="Could not parse Drive folder id")

    output_name = title if title.casefold().endswith(".mp4") else f"{title}.mp4"
    record_type = fields.get(FIELD_TYPE)
    video_type = record_type if isinstance(record_type, str) and record_type.strip() else None
    combined_existing = fields.get(FIELD_COMBINED_MEDIA_FILE)
    has_combined = isinstance(combined_existing, str) and bool(combined_existing.strip())
    subtitles_existing = fields.get(FIELD_TRANSLATED_SUBTITLES)
    has_subtitles = isinstance(subtitles_existing, str) and bool(subtitles_existing.strip())
    if dry_run:
        if has_combined and has_subtitles:
            return ActionResult(action=action, success=True, message="Already combined")
        if not has_combined:
            check = check_mixable_media(drive, pkg_folder_id, video_type=video_type)
            if not check.ok:
                return ActionResult(
                    action=action,
                    success=False,
                    message=format_mix_media_check(check),
                )
            return ActionResult(
                action=action,
                success=True,
                message=(
                    f"Would combine media -> {output_name} and align subtitles; "
                    f"{format_mix_media_check(check)}"
                ),
            )
        return ActionResult(
            action=action,
            success=True,
            message="Would align Bulgarian subtitles using existing combined media",
        )

    work_dir = config.work_dir / action.record_id
    messages: list[str] = []
    audio_paths: list[Path] = []
    if not has_combined:
        created = mix_folder_media_to_drive(
            drive,
            pkg_folder_id=pkg_folder_id,
            output_parent_id=output_parent_id,
            output_name=output_name,
            work_dir=work_dir,
            dry_run=False,
            video_type=video_type,
        )
        audio_paths = list(created.local_audio_paths)
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
        messages.append(
            f"Combined media uploaded: {drive_url} (local: {work_dir / output_name})"
        )
    else:
        messages.append("Combined media already present")

    if has_subtitles:
        return ActionResult(action=action, success=True, message="; ".join(messages))

    smartcat_link = fields.get(FIELD_TRANSLATION_RESOURCES)
    if not isinstance(smartcat_link, str) or not smartcat_link.strip():
        messages.append("Skipped aligned subtitles (no Translation resources)")
        return ActionResult(action=action, success=True, message="; ".join(messages))

    dialogue = pick_dialogue_audio_path(audio_paths)
    if dialogue is None:
        if audio_paths:
            messages.append(
                "Aligned subtitles failed: mixed stems have no dialogue audio file"
            )
            return ActionResult(action=action, success=False, message="; ".join(messages))
        from catalog_parser.drive_combine import download_drive_file, find_stems_media

        stems = find_stems_media(drive, drive_link)
        work_dir.mkdir(parents=True, exist_ok=True)
        dialogue = download_drive_file(
            drive,
            stems.audio.id,
            work_dir / stems.audio.name,
        )
        messages.append(f"Downloaded dialogue audio {stems.audio.name!r} for subtitle align")

    from catalog_parser.smartcat import SmartcatError
    from catalog_parser.translation.aligned_subtitles import generate_aligned_subtitles
    from catalog_parser.translation.srt_retime import SrtRetimeError
    from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError

    root = project_root if project_root is not None else config.work_dir.parent
    try:
        subtitle_url = generate_aligned_subtitles(
            airtable=airtable,
            record_id=action.record_id,
            title=title,
            smartcat_link=smartcat_link,
            audio_path=dialogue,
            work_dir=work_dir,
            project_root=root,
            drive=GoogleDriveClient(drive),
        )
    except (SrtRetimeError, SmartcatError, GoogleDriveError, ValueError) as exc:
        messages.append(f"Aligned subtitles failed: {exc}")
        return ActionResult(action=action, success=False, message="; ".join(messages))

    if table_cache is not None:
        table_cache.update_fields(
            action.record_id,
            {FIELD_TRANSLATED_SUBTITLES: subtitle_url},
        )
    messages.append(f"Aligned subtitles uploaded: {subtitle_url}")
    return ActionResult(action=action, success=True, message="; ".join(messages))


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
        dry_run=dry_run,
    )
    if dry_run:
        return ActionResult(
            action=action,
            success=True,
            message=(
                f"Would ingest {len(created_ids)} {action.ingest_type}(s) "
                f"for translator {action.translator_name!r}"
                if created_ids
                else f"No eligible catalog row found for {action.translator_name!r}"
            ),
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
        preferred_editors_by_translator = {
            translator.name: translator.preferred_editor
            for translator in config.translators
            if translator.preferred_editor
        }
        translator = fields.get(FIELD_TRANSLATOR)
        preferred_editor = None
        if isinstance(translator, str) and translator.strip():
            preferred_editor = preferred_editors_by_translator.get(translator.strip())
        records_for_utilization = (
            table_cache.records if table_cache is not None else airtable.list_records()
        )
        chosen_name = choose_editor(
            records_for_utilization,
            record_type=record_type,
            editors=editor_slots,
            preferred_editor=preferred_editor,
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


def _assign_timing_editor(
    action: WorkflowAction,
    *,
    airtable: AirtableClient,
    config: WorkflowConfig,
    table_cache: TableCache | None,
    dry_run: bool,
    drive_service: Resource | None = None,
    docs_service: Resource | None = None,
    project_root: Path | None = None,
) -> ActionResult:
    if not action.record_id:
        return ActionResult(action=action, success=False, message="Missing record_id")

    record = table_cache.get(action.record_id) if table_cache is not None else None
    if record is None:
        record = airtable.get_record(action.record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        return ActionResult(action=action, success=False, message="Record has no fields")

    if action.timing_editor_name:
        chosen_name = action.timing_editor_name
    else:
        record_type = fields.get("Type")
        timing_editor_slots = [
            (
                timing_editor.name,
                timing_editor.weekly_capacity_reels,
                timing_editor.preferred_timing_type,
            )
            for timing_editor in config.timing_editors
        ]
        records_for_utilization = (
            table_cache.records if table_cache is not None else airtable.list_records()
        )
        chosen_name = choose_timing_editor(
            records_for_utilization,
            record_type=record_type,
            timing_editors=timing_editor_slots,
        )
        if chosen_name is None:
            return ActionResult(
                action=action,
                success=True,
                message="Skipped: no eligible timing editors for this type",
            )

    if dry_run:
        return ActionResult(
            action=action,
            success=True,
            message=f"Would assign timing editor {chosen_name!r}",
        )
    airtable.update_record_fields(action.record_id, {FIELD_TIMING_EDITOR: chosen_name})
    if table_cache is not None:
        table_cache.update_fields(action.record_id, {FIELD_TIMING_EDITOR: chosen_name})

    message = f"Assigned timing editor {chosen_name!r}"
    root = project_root
    if root is None and config.work_dir.is_absolute():
        root = config.work_dir.parent
    if root is not None:
        try:
            from catalog_parser.translation.corpus_append import append_record_to_corpus

            corpus_result = append_record_to_corpus(
                record,
                airtable=airtable,
                project_root=root,
                drive_service=drive_service,
                docs_service=docs_service,
            )
            message = f"{message}; corpus: {corpus_result.summary}"
        except Exception as exc:  # noqa: BLE001 — assignment already succeeded
            message = f"{message}; corpus skipped: {exc}"

    return ActionResult(action=action, success=True, message=message)
