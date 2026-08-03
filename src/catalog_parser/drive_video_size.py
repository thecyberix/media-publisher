from __future__ import annotations

import tempfile
from pathlib import Path

from googleapiclient.discovery import Resource

from catalog_parser.drive_combine import download_drive_file
from catalog_parser.drive_mix import find_video_and_audio_subfolder


def video_size_from_drive_file_metadata(
    drive_service: Resource,
    file_id: str,
) -> tuple[int, int] | None:
    response = (
        drive_service.files()
        .get(
            fileId=file_id,
            fields="videoMediaMetadata(width,height)",
            supportsAllDrives=True,
        )
        .execute()
    )
    if not isinstance(response, dict):
        return None

    metadata = response.get("videoMediaMetadata")
    if not isinstance(metadata, dict):
        return None

    width = metadata.get("width")
    height = metadata.get("height")
    if not width or not height:
        return None

    width_value = int(width)
    height_value = int(height)
    if width_value <= 0 or height_value <= 0:
        return None
    return width_value, height_value


def video_size_from_drive_file(
    drive_service: Resource,
    file_id: str,
    *,
    file_name: str | None = None,
) -> tuple[int, int] | None:
    size = video_size_from_drive_file_metadata(drive_service, file_id)
    if size is not None:
        return size

    from media_publisher.video_duration import probe_local_video_size

    suffix = Path(file_name or "video.mp4").suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix="drive-video-size-") as tmp:
        destination = Path(tmp) / f"probe{suffix}"
        try:
            download_drive_file(drive_service, file_id, destination)
        except Exception:
            return None
        return probe_local_video_size(destination)


def video_size_from_pkg_folder(
    drive_service: Resource,
    pkg_folder_id: str,
    *,
    video_type: str | None = None,
) -> tuple[int, int] | None:
    try:
        media = find_video_and_audio_subfolder(
            drive_service,
            pkg_folder_id,
            video_type=video_type,
        )
    except Exception:
        return None
    return video_size_from_drive_file(
        drive_service,
        media.video.id,
        file_name=media.video.name,
    )
