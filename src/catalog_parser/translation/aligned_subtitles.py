"""Align Smartcat Bulgarian SRT to dialogue audio and store the file on Drive."""
from __future__ import annotations

import re
from pathlib import Path

from catalog_parser.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_TRANSLATED_SUBTITLES,
)
from catalog_parser.drive_docs import drive_file_view_url
from catalog_parser.smartcat import (
    SmartcatError,
    configured_target_language,
    parse_pkg_sm_link,
    parse_smartcat_resource_link,
    resolve_language_id,
)
from catalog_parser.smartcat_export import (
    WEB_EXPORT_TYPE_SOURCE,
    WEB_EXPORT_TYPE_TARGET,
    WEB_SEGMENT_EXPORT_MODE_SOURCE,
    WEB_SEGMENT_EXPORT_MODE_TARGET,
    build_cookie_client_from_env,
    export_document_srt_via_web_api,
)
from catalog_parser.translation.srt import Cue, apply_cue_timings, parse_srt, write_srt
from catalog_parser.translation.srt_retime import (
    SrtRetimeError,
    align_words_whisperx,
    audio_duration_ms,
    detect_hour_offset_ms,
    retime_cues,
    retimed_cues,
    shift_cues,
)
from media_publisher.sources.drive_layout import FOLDER_SUBTITLES, ensure_named_folder
from media_publisher.sources.google_drive import GoogleDriveClient

UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*]+')


def slug_title(title: str) -> str:
    cleaned = UNSAFE_FILENAME.sub(" ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or "video"


def resolve_smartcat_document(
    *,
    title: str,
    smartcat_link: str,
    project_root: Path,
) -> tuple[str, int]:
    cookie_client = build_cookie_client_from_env(project_root=project_root)
    parsed_editor = parse_smartcat_resource_link(smartcat_link)
    parsed_project = parse_pkg_sm_link(smartcat_link)
    language_id = int(resolve_language_id(configured_target_language()))
    if parsed_editor is not None and parsed_editor.target_language_id is not None:
        language_id = int(parsed_editor.target_language_id)

    document_id = parsed_editor.document_id if parsed_editor else None
    if not document_id:
        project_id = (
            parsed_project.project_id
            if parsed_project is not None
            else (parsed_editor.project_id if parsed_editor else None)
        )
        search = (
            parsed_project.search
            if parsed_project is not None
            else ((parsed_editor.search if parsed_editor else None) or title)
        )
        if not project_id:
            raise SmartcatError(f"Could not parse Smartcat link: {smartcat_link!r}")
        document = cookie_client.find_document(project_id, search=search, title=title)
        document_id = document.get("id") if isinstance(document, dict) else None
    if not isinstance(document_id, str) or not document_id:
        raise SmartcatError(f"Could not resolve Smartcat document for {title!r}")
    return document_id, language_id


def export_smartcat_srt(
    *,
    title: str,
    smartcat_link: str,
    project_root: Path,
    export_type: int,
    segment_export_mode: int,
) -> str:
    cookie_client = build_cookie_client_from_env(project_root=project_root)
    document_id, language_id = resolve_smartcat_document(
        title=title, smartcat_link=smartcat_link, project_root=project_root
    )
    return export_document_srt_via_web_api(
        cookie_client,
        document_id,
        language_id,
        export_type=export_type,
        segment_export_mode=segment_export_mode,
    )


def retime_english_cues_to_audio(
    source_srt: str,
    audio_path: Path,
    *,
    language: str = "en",
    device: str | None = None,
) -> list[Cue]:
    cues = parse_srt(source_srt)
    if not cues:
        raise SrtRetimeError("English SRT parsed to zero cues")
    duration_ms = audio_duration_ms(audio_path)
    offset_ms = detect_hour_offset_ms(cues, duration_ms)
    baseline = shift_cues(cues, -offset_ms)
    words = align_words_whisperx(
        audio_path,
        baseline,
        language=language,
        device=device,
    )
    return retimed_cues(retime_cues(baseline, words))


def upload_aligned_bulgarian_srt(
    *,
    airtable: AirtableClient,
    record_id: str,
    title: str,
    retimed_cues: list[Cue],
    bulgarian_srt: str,
    srt_path: Path,
    drive: GoogleDriveClient,
) -> str:
    bg_cues = parse_srt(bulgarian_srt)
    if not bg_cues:
        raise SrtRetimeError("Bulgarian SRT parsed to zero cues")
    aligned = apply_cue_timings(retimed_cues, bg_cues)
    srt_path.write_text(write_srt(aligned), encoding="utf-8", newline="\n")

    filename = f"{slug_title(title)}.bg.srt"
    catalog_subtitles_id = ensure_named_folder(drive, FOLDER_SUBTITLES)
    parent_id = drive.ensure_folder(catalog_subtitles_id, slug_title(title)).id
    uploaded = drive.upload_or_update_file(
        parent_id,
        srt_path,
        name=filename,
        mime_type="application/x-subrip",
    )
    file_url = drive_file_view_url(uploaded.file.id)
    try:
        airtable.ensure_url_field(FIELD_TRANSLATED_SUBTITLES)
    except AirtableError:
        pass
    airtable.update_record_fields(record_id, {FIELD_TRANSLATED_SUBTITLES: file_url})
    return file_url


def generate_aligned_subtitles(
    *,
    airtable: AirtableClient,
    record_id: str,
    title: str,
    smartcat_link: str,
    audio_path: Path,
    work_dir: Path,
    project_root: Path,
    drive: GoogleDriveClient,
) -> str:
    """Export EN/BG SRT, align EN to local dialogue audio, upload BG with those times."""
    work_dir.mkdir(parents=True, exist_ok=True)
    english_srt = export_smartcat_srt(
        title=title,
        smartcat_link=smartcat_link,
        project_root=project_root,
        export_type=WEB_EXPORT_TYPE_SOURCE,
        segment_export_mode=WEB_SEGMENT_EXPORT_MODE_SOURCE,
    )
    retimed = retime_english_cues_to_audio(english_srt, audio_path)
    bulgarian_srt = export_smartcat_srt(
        title=title,
        smartcat_link=smartcat_link,
        project_root=project_root,
        export_type=WEB_EXPORT_TYPE_TARGET,
        segment_export_mode=WEB_SEGMENT_EXPORT_MODE_TARGET,
    )
    return upload_aligned_bulgarian_srt(
        airtable=airtable,
        record_id=record_id,
        title=title,
        retimed_cues=retimed,
        bulgarian_srt=bulgarian_srt,
        srt_path=work_dir / "bulgarian.aligned.srt",
        drive=drive,
    )
