from __future__ import annotations

from typing import Any

from googleapiclient.discovery import Resource

from catalog_parser.drive_docs import extract_drive_folder_id

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SHORTCUT_MIME_TYPE = "application/vnd.google-apps.shortcut"
DEFAULT_MEDIA_SUBFOLDER_DEPTH = 1


def is_audio_mime_type(mime_type: str) -> bool:
    return mime_type.startswith("audio/")


def is_video_mime_type(mime_type: str) -> bool:
    return mime_type.startswith("video/")


def list_folder_children(
    drive_service: Resource,
    folder_id: str,
) -> list[dict[str, Any]]:
    response = (
        drive_service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = response.get("files", [])
    if not isinstance(files, list):
        return []
    return [file_info for file_info in files if isinstance(file_info, dict)]


def resolve_drive_item(
    drive_service: Resource,
    item: dict[str, Any],
) -> dict[str, Any]:
    mime_type = item.get("mimeType")
    file_id = item.get("id")
    if mime_type != SHORTCUT_MIME_TYPE or not isinstance(file_id, str):
        return item

    shortcut = (
        drive_service.files()
        .get(
            fileId=file_id,
            fields="shortcutDetails(targetId,targetMimeType)",
            supportsAllDrives=True,
        )
        .execute()
    )
    target_id = shortcut.get("shortcutDetails", {}).get("targetId")
    if not isinstance(target_id, str) or not target_id:
        return item

    target = (
        drive_service.files()
        .get(
            fileId=target_id,
            fields="id,name,mimeType",
            supportsAllDrives=True,
        )
        .execute()
    )
    if not isinstance(target, dict):
        return item
    return target


def find_media_subfolder_ids(
    drive_service: Resource,
    folder_id: str,
) -> list[str]:
    subfolder_ids: list[str] = []
    for item in list_folder_children(drive_service, folder_id):
        resolved = resolve_drive_item(drive_service, item)
        if resolved.get("mimeType") != FOLDER_MIME_TYPE:
            continue
        resolved_id = resolved.get("id")
        if isinstance(resolved_id, str) and resolved_id:
            subfolder_ids.append(resolved_id)
    return subfolder_ids


def folder_contains_audio_and_video(
    drive_service: Resource,
    folder_id: str,
    *,
    max_subfolder_depth: int = DEFAULT_MEDIA_SUBFOLDER_DEPTH,
) -> bool:
    has_audio = False
    has_video = False

    def scan(current_folder_id: str, depth: int) -> None:
        nonlocal has_audio, has_video
        if has_audio and has_video:
            return

        for item in list_folder_children(drive_service, current_folder_id):
            resolved = resolve_drive_item(drive_service, item)
            mime_type = resolved.get("mimeType")
            if not isinstance(mime_type, str):
                continue

            if is_audio_mime_type(mime_type):
                has_audio = True
            elif is_video_mime_type(mime_type):
                has_video = True
            elif mime_type == FOLDER_MIME_TYPE and depth < max_subfolder_depth:
                resolved_id = resolved.get("id")
                if isinstance(resolved_id, str) and resolved_id:
                    scan(resolved_id, depth + 1)

            if has_audio and has_video:
                return

    scan(folder_id, 0)
    return has_audio and has_video


def pkg_link_has_stems_media(
    drive_service: Resource,
    pkg_link: str,
) -> bool:
    folder_id = extract_drive_folder_id(pkg_link)
    if folder_id is None:
        return False

    media_subfolder_ids = find_media_subfolder_ids(drive_service, folder_id)
    if not media_subfolder_ids:
        return False

    return any(
        folder_contains_audio_and_video(drive_service, media_subfolder_id)
        for media_subfolder_id in media_subfolder_ids
    )


def record_has_stems_media(
    drive_service: Resource,
    record: dict[str, Any],
    *,
    folder_link_field: str = "pkgLink",
) -> bool:
    folder_link = record.get(folder_link_field)
    if not isinstance(folder_link, str) or not folder_link.strip():
        return False
    return pkg_link_has_stems_media(drive_service, folder_link)
