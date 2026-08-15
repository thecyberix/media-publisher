"""Resolve named Drive folders under the DRIVE_URL parent."""

from __future__ import annotations

import os
import re
from typing import Any

from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError

FOLDER_COMBINED_MEDIA_FILES = "Combined Media Files"
FOLDER_EVENTS = "Events"
FOLDER_OVERRIDES = "Overrides"
FOLDER_QUOTES = "Quotes"
FOLDER_THUMBNAILS_FOR_APPROVAL = "Thumbnails for approval"

DRIVE_FOLDER_PATTERN = re.compile(r"/folders/([a-zA-Z0-9_-]+)")


def extract_drive_folder_id(value: str) -> str | None:
    text = (value or "").strip()
    if not text:
        return None
    match = DRIVE_FOLDER_PATTERN.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]+", text):
        return text
    return None


def drive_folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def drive_url_from_env() -> str:
    return os.getenv("DRIVE_URL", "").strip()


def require_drive_root_id(drive_url: str = "") -> str:
    raw = (drive_url or drive_url_from_env()).strip()
    folder_id = extract_drive_folder_id(raw)
    if not folder_id:
        raise GoogleDriveError(
            "DRIVE_URL is required (parent Google Drive folder URL or id)"
        )
    return folder_id


def as_drive_client(drive: GoogleDriveClient | Any) -> GoogleDriveClient:
    if isinstance(drive, GoogleDriveClient) or hasattr(drive, "find_child_folder"):
        return drive
    return GoogleDriveClient(drive)


def resolve_named_folder(
    drive: GoogleDriveClient | Any,
    folder_name: str,
    *,
    drive_url: str = "",
) -> str:
    """Return a child folder id under DRIVE_URL."""
    client = as_drive_client(drive)
    root_id = require_drive_root_id(drive_url)
    found = client.find_child_folder(root_id, folder_name)
    if found is None:
        raise GoogleDriveError(
            f"Drive folder {folder_name!r} not found under DRIVE_URL ({root_id})"
        )
    return found.id


def resolve_combined_media_files_id(
    drive: GoogleDriveClient | Any,
    *,
    drive_url: str = "",
) -> str:
    return resolve_named_folder(
        drive, FOLDER_COMBINED_MEDIA_FILES, drive_url=drive_url
    )


def resolve_events_folder_id(
    drive: GoogleDriveClient | Any,
    *,
    drive_url: str = "",
) -> str:
    return resolve_named_folder(drive, FOLDER_EVENTS, drive_url=drive_url)


def resolve_overrides_folder_id(
    drive: GoogleDriveClient | Any,
    *,
    drive_url: str = "",
) -> str:
    return resolve_named_folder(drive, FOLDER_OVERRIDES, drive_url=drive_url)


def resolve_quotes_folder_id(
    drive: GoogleDriveClient | Any,
    *,
    drive_url: str = "",
) -> str:
    return resolve_named_folder(drive, FOLDER_QUOTES, drive_url=drive_url)


def resolve_thumbnails_for_approval_id(
    drive: GoogleDriveClient | Any,
    *,
    drive_url: str = "",
) -> str:
    return resolve_named_folder(
        drive, FOLDER_THUMBNAILS_FOR_APPROVAL, drive_url=drive_url
    )
