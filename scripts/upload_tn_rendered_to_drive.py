"""Upload rendered TN thumbnails from downloads/tn-rendered to a Drive folder."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from googleapiclient.http import MediaFileUpload

from catalog_parser.__main__ import load_env_file
from catalog_parser.auth import get_drive_service, get_drive_service_noninteractive
from catalog_parser.drive_combine import upload_drive_file
from catalog_parser.runtime_env import materialize_credentials

DEFAULT_SOURCE_DIR = PROJECT_ROOT / "downloads" / "tn-rendered"
DEFAULT_FOLDER_ID = "1wylc-TI2YjOxTaLeaojgSFJNY0LmEJFo"


def drive_upload_name(path: Path) -> str:
    return path.name.replace(".tn-render", "")


def list_existing_names(drive, folder_id: str) -> dict[str, str]:
    response = (
        drive.files()
        .list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=1000,
        )
        .execute()
    )
    existing: dict[str, str] = {}
    for item in response.get("files", []):
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        file_id = item.get("id")
        if isinstance(name, str) and isinstance(file_id, str):
            existing[name] = file_id
    return existing


def update_drive_file(drive, file_id: str, source_path: Path) -> None:
    media = MediaFileUpload(str(source_path), mimetype="image/jpeg", resumable=True)
    drive.files().update(
        fileId=file_id,
        media_body=media,
        supportsAllDrives=True,
    ).execute()


def build_drive_service(credentials: Path | None, token: Path | None):
    if credentials is not None and token is not None and credentials.is_file():
        return get_drive_service(credentials, token, use_console=True)
    return get_drive_service_noninteractive()


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    materialize_credentials(PROJECT_ROOT)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--folder-id", default=DEFAULT_FOLDER_ID)
    parser.add_argument(
        "--credentials",
        type=Path,
        default=PROJECT_ROOT / "credentials.json",
        help="OAuth client credentials.json (optional; uses service account when absent)",
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=PROJECT_ROOT / "token.json",
        help="OAuth token path (optional)",
    )
    parser.add_argument("--force", action="store_true", help="Replace files with matching names")
    args = parser.parse_args()

    source_dir = args.source_dir
    if not source_dir.is_dir():
        print(f"Source directory not found: {source_dir}")
        return 1

    files = sorted(source_dir.glob("*.jpg"))
    if not files:
        print(f"No JPG files found in {source_dir}")
        return 1

    drive = build_drive_service(args.credentials, args.token)
    existing = list_existing_names(drive, args.folder_id)

    print(f"=== Upload TN rendered thumbnails ({len(files)} files) ===")
    print(f"Source: {source_dir.resolve()}")
    print(f"Folder: https://drive.google.com/drive/folders/{args.folder_id}")
    print(f"Existing in folder: {len(existing)}")
    print()

    uploaded = 0
    updated = 0
    skipped = 0
    failed: list[tuple[str, str]] = []

    for path in files:
        drive_name = drive_upload_name(path)
        print(f"{path.name} -> {drive_name}")

        if drive_name in existing and not args.force:
            skipped += 1
            print("  status: SKIPPED (already exists; use --force to replace)")
            print()
            continue

        try:
            if drive_name in existing:
                update_drive_file(drive, existing[drive_name], path)
                updated += 1
                print("  status: UPDATED")
            else:
                result = upload_drive_file(
                    drive,
                    args.folder_id,
                    path,
                    name=drive_name,
                    mime_type="image/jpeg",
                )
                uploaded += 1
                existing[drive_name] = result.id
                print(f"  status: UPLOADED ({result.id})")
        except Exception as exc:
            failed.append((path.name, str(exc)))
            print(f"  status: FAILED ({exc})")
        print()

    print("=== Summary ===")
    print(f"Uploaded: {uploaded}")
    print(f"Updated:  {updated}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {len(failed)}")
    if failed:
        print()
        for name, reason in failed:
            print(f"  - {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
