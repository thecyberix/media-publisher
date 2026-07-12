"""Probe Drive folder structure for stems/media check."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from catalog_parser.__main__ import DEFAULT_CREDENTIALS, DEFAULT_TOKEN, load_env_file
from catalog_parser.auth import get_drive_service

load_env_file(Path(__file__).resolve().parents[1] / ".env")
folder_id = sys.argv[1] if len(sys.argv) > 1 else "1-F_9awPFn6ZUam22lkpyrR_a_TnnIYVy"
drive = get_drive_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN)


SHORTCUT_MIME = "application/vnd.google-apps.shortcut"


def resolve_shortcut(file_meta: dict) -> dict:
    if file_meta.get("mimeType") != SHORTCUT_MIME:
        return file_meta
    details = (
        drive.files()
        .get(
            fileId=file_meta["id"],
            fields="shortcutDetails(targetId,targetMimeType)",
            supportsAllDrives=True,
        )
        .execute()
    )
    target_id = details.get("shortcutDetails", {}).get("targetId")
    if not target_id:
        return file_meta
    return (
        drive.files()
        .get(fileId=target_id, fields="id,name,mimeType", supportsAllDrives=True)
        .execute()
    )


def list_folder(fid: str, indent: int = 0) -> None:
    r = (
        drive.files()
        .list(
            q=f"'{fid}' in parents and trashed=false",
            fields="files(id,name,mimeType)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    for f in sorted(r.get("files", []), key=lambda x: x.get("name", "")):
        resolved = resolve_shortcut(f)
        mime = resolved.get("mimeType", "")[:50]
        name = f.get("name", "")
        if resolved.get("id") != f.get("id"):
            name = f"{name} -> {resolved.get('name', '')}"
        print("  " * indent + f"{mime} | {name}")
        if resolved.get("mimeType") == "application/vnd.google-apps.folder":
            list_folder(resolved["id"], indent + 1)


list_folder(folder_id)
