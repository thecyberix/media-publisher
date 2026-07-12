from __future__ import annotations

import io
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from googleapiclient.discovery import Resource
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from catalog_parser.auth import service_account_email_hint

from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.ffmpeg_bundle import ensure_ffmpeg_bundled
from catalog_parser.drive_media import (
    find_media_subfolder_ids,
    is_audio_mime_type,
    is_video_mime_type,
    list_folder_children,
    resolve_drive_item,
)

DEFAULT_COMBINED_VIDEO_NAME = "All Video (combined).mp4"
DEFAULT_DIALOGUE_AUDIO_NAME = "All Dialogue.wav"
DEFAULT_SOURCE_VIDEO_NAME = "All Video.mp4"
DEFAULT_FFMPEG = "ffmpeg"


class DriveCombineError(RuntimeError):
    pass


def _drive_folder_access_message(folder_id: str) -> str:
    hint = service_account_email_hint()
    return (
        f"Drive folder {folder_id!r} was not found or is not accessible to the current "
        f"Google credentials.{hint} Share that folder with the service account as Editor "
        "in Google Drive, then retry."
    )


def verify_drive_output_folder_access(
    drive_service: Resource,
    folder_id: str,
) -> None:
    try:
        drive_service.files().get(
            fileId=folder_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            raise DriveCombineError(_drive_folder_access_message(folder_id)) from exc
        raise DriveCombineError(
            f"Could not access Drive output folder {folder_id!r}: {exc}"
        ) from exc


@dataclass(frozen=True)
class DriveMediaFile:
    id: str
    name: str
    mime_type: str
    parent_id: str


@dataclass(frozen=True)
class StemsMedia:
    pkg_folder_id: str
    media_folder_id: str
    audio: DriveMediaFile
    video: DriveMediaFile
    output_parent_id: str
    output_name: str = DEFAULT_COMBINED_VIDEO_NAME


def _drive_file(
    item: dict[str, Any],
    *,
    parent_id: str,
) -> DriveMediaFile | None:
    file_id = item.get("id")
    name = item.get("name")
    mime_type = item.get("mimeType")
    if not isinstance(file_id, str) or not file_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(mime_type, str) or not mime_type:
        return None
    return DriveMediaFile(
        id=file_id,
        name=name,
        mime_type=mime_type,
        parent_id=parent_id,
    )


def _pick_audio_file(files: list[DriveMediaFile]) -> DriveMediaFile | None:
    audio_files = [item for item in files if is_audio_mime_type(item.mime_type)]
    if not audio_files:
        return None

    for preferred_name in (DEFAULT_DIALOGUE_AUDIO_NAME,):
        for item in audio_files:
            if item.name.casefold() == preferred_name.casefold():
                return item

    dialogue_matches = [
        item for item in audio_files if "dialogue" in item.name.casefold()
    ]
    if len(dialogue_matches) == 1:
        return dialogue_matches[0]
    if dialogue_matches:
        return sorted(dialogue_matches, key=lambda item: item.name.casefold())[0]

    return sorted(audio_files, key=lambda item: item.name.casefold())[0]


def _pick_video_file(files: list[DriveMediaFile]) -> DriveMediaFile | None:
    video_files = [item for item in files if is_video_mime_type(item.mime_type)]
    if not video_files:
        return None

    for preferred_name in (DEFAULT_SOURCE_VIDEO_NAME,):
        for item in video_files:
            if item.name.casefold() == preferred_name.casefold():
                return item

    if len(video_files) == 1:
        return video_files[0]
    return sorted(video_files, key=lambda item: item.name.casefold())[0]


def _collect_media_files(
    drive_service: Resource,
    folder_id: str,
    *,
    max_subfolder_depth: int = 1,
) -> list[DriveMediaFile]:
    collected: list[DriveMediaFile] = []

    def scan(current_folder_id: str, depth: int) -> None:
        for item in list_folder_children(drive_service, current_folder_id):
            resolved = resolve_drive_item(drive_service, item)
            mime_type = resolved.get("mimeType")
            if not isinstance(mime_type, str):
                continue

            resolved_id = resolved.get("id")
            if not isinstance(resolved_id, str) or not resolved_id:
                continue

            if is_audio_mime_type(mime_type) or is_video_mime_type(mime_type):
                media_file = _drive_file(resolved, parent_id=current_folder_id)
                if media_file is not None:
                    collected.append(media_file)
            elif mime_type == "application/vnd.google-apps.folder" and depth < max_subfolder_depth:
                scan(resolved_id, depth + 1)

    scan(folder_id, 0)
    return collected


def find_stems_media(
    drive_service: Resource,
    pkg_link: str,
    *,
    output_name: str = DEFAULT_COMBINED_VIDEO_NAME,
) -> StemsMedia:
    folder_id = extract_drive_folder_id(pkg_link)
    if folder_id is None:
        raise DriveCombineError(f"Could not parse Drive folder id from {pkg_link!r}")

    media_subfolder_ids = find_media_subfolder_ids(drive_service, folder_id)
    if not media_subfolder_ids:
        raise DriveCombineError(
            f"No media subfolder found under Drive folder {folder_id!r}"
        )

    last_error: DriveCombineError | None = None
    for media_folder_id in media_subfolder_ids:
        media_files = _collect_media_files(drive_service, media_folder_id)
        audio = _pick_audio_file(media_files)
        video = _pick_video_file(media_files)
        if audio is None or video is None:
            last_error = DriveCombineError(
                f"Media folder {media_folder_id!r} is missing "
                f"{'audio' if audio is None else 'video'} file"
            )
            continue

        return StemsMedia(
            pkg_folder_id=folder_id,
            media_folder_id=media_folder_id,
            audio=audio,
            video=video,
            output_parent_id=media_folder_id,
            output_name=output_name,
        )

    raise last_error or DriveCombineError(
        f"No usable audio/video pair found under Drive folder {folder_id!r}"
    )


def folder_has_combined_video(
    drive_service: Resource,
    parent_id: str,
    *,
    output_name: str = DEFAULT_COMBINED_VIDEO_NAME,
) -> DriveMediaFile | None:
    for item in list_folder_children(drive_service, parent_id):
        resolved = resolve_drive_item(drive_service, item)
        if resolved.get("name") == output_name and is_video_mime_type(
            str(resolved.get("mimeType", ""))
        ):
            media_file = _drive_file(resolved, parent_id=parent_id)
            if media_file is not None:
                return media_file
    return None


def resolve_ffmpeg_path(ffmpeg_path: str | None = None) -> str:
    if ffmpeg_path:
        candidate = Path(ffmpeg_path)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(ffmpeg_path)
        if found:
            return found
        raise DriveCombineError(f"ffmpeg not found at {ffmpeg_path!r}")

    found = shutil.which(DEFAULT_FFMPEG)
    if found:
        return found

    try:
        bundled = ensure_ffmpeg_bundled()
        return str(bundled.ffmpeg_path)
    except Exception as exc:
        raise DriveCombineError(
            "ffmpeg is required to combine video and audio but was not found on PATH "
            f"and could not be bundled automatically: {exc}"
        ) from exc


def download_drive_file(
    drive_service: Resource,
    file_id: str,
    destination: Path,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    destination.write_bytes(buffer.getvalue())
    return destination


def combine_video_and_audio(
    video_path: Path,
    audio_path: Path,
    output_path: Path,
    *,
    ffmpeg_path: str | None = None,
) -> Path:
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(audio_path),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DriveCombineError(f"Failed to run ffmpeg: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise DriveCombineError(f"ffmpeg combine failed: {detail}")

    if not output_path.exists():
        raise DriveCombineError(f"ffmpeg did not create output file: {output_path}")
    return output_path


def upload_drive_file(
    drive_service: Resource,
    parent_id: str,
    source_path: Path,
    *,
    name: str,
    mime_type: str = "video/mp4",
) -> DriveMediaFile:
    metadata = {"name": name, "parents": [parent_id]}
    media = MediaFileUpload(str(source_path), mimetype=mime_type, resumable=True)
    try:
        created = (
            drive_service.files()
            .create(
                body=metadata,
                media_body=media,
                fields="id,name,mimeType,parents",
                supportsAllDrives=True,
            )
            .execute()
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            raise DriveCombineError(_drive_folder_access_message(parent_id)) from exc
        raise DriveCombineError(f"Drive upload failed: {exc}") from exc
    file_id = created.get("id")
    if not isinstance(file_id, str) or not file_id:
        raise DriveCombineError("Drive upload did not return a file id")
    return DriveMediaFile(
        id=file_id,
        name=str(created.get("name") or name),
        mime_type=str(created.get("mimeType") or mime_type),
        parent_id=parent_id,
    )


def combine_stems_media_to_drive(
    drive_service: Resource,
    pkg_link: str,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    output_name: str = DEFAULT_COMBINED_VIDEO_NAME,
    force: bool = False,
    dry_run: bool = False,
) -> DriveMediaFile:
    stems = find_stems_media(drive_service, pkg_link, output_name=output_name)
    existing = folder_has_combined_video(
        drive_service,
        stems.output_parent_id,
        output_name=output_name,
    )
    if existing is not None and not force:
        raise DriveCombineError(
            f"Combined video already exists in Drive: {existing.name!r} "
            f"(id={existing.id}). Use --force to replace."
        )

    if dry_run:
        return DriveMediaFile(
            id="dry-run",
            name=output_name,
            mime_type="video/mp4",
            parent_id=stems.output_parent_id,
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    video_path = work_dir / stems.video.name
    audio_path = work_dir / stems.audio.name
    output_path = work_dir / output_name

    download_drive_file(drive_service, stems.video.id, video_path)
    download_drive_file(drive_service, stems.audio.id, audio_path)
    combine_video_and_audio(
        video_path,
        audio_path,
        output_path,
        ffmpeg_path=ffmpeg_path,
    )

    if existing is not None and force:
        drive_service.files().delete(
            fileId=existing.id,
            supportsAllDrives=True,
        ).execute()

    return upload_drive_file(
        drive_service,
        stems.output_parent_id,
        output_path,
        name=output_name,
    )
