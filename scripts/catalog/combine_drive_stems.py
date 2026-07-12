"""Download stems video+audio from a Drive pkg folder, mux with ffmpeg, upload back."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.__main__ import DEFAULT_CREDENTIALS, DEFAULT_TOKEN, load_env_file
from catalog_parser.auth import get_drive_service
from catalog_parser.drive_combine import (
    DriveCombineError,
    combine_stems_media_to_drive,
    find_stems_media,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Combine All Video.mp4 + All Dialogue.wav from a Drive pkg folder "
            "and upload the result back to the media (Stems) folder."
        )
    )
    parser.add_argument(
        "--pkg-link",
        help="Drive folder URL from Airtable Video Folder",
    )
    parser.add_argument(
        "--folder-id",
        help="Drive folder id (alternative to --pkg-link)",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "drive-combine",
        help="Local temp directory for downloads and ffmpeg output",
    )
    parser.add_argument(
        "--ffmpeg",
        default=None,
        help="Path to ffmpeg executable (defaults to PATH)",
    )
    parser.add_argument(
        "--output-name",
        default=None,
        help="Uploaded file name (default: All Video (combined).mp4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only resolve source files; do not download, mux, or upload",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing combined video in Drive",
    )
    args = parser.parse_args()

    if bool(args.pkg_link) == bool(args.folder_id):
        if not args.pkg_link and not args.folder_id:
            parser.error("Provide exactly one of --pkg-link or --folder-id")
        if args.pkg_link and args.folder_id:
            parser.error("Provide only one of --pkg-link or --folder-id")

    pkg_link = args.pkg_link
    if args.folder_id:
        pkg_link = f"https://drive.google.com/drive/folders/{args.folder_id}"

    load_env_file(PROJECT_ROOT / ".env")
    drive = get_drive_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN)

    kwargs = {}
    if args.output_name:
        kwargs["output_name"] = args.output_name

    try:
        stems = find_stems_media(drive, pkg_link, **kwargs)
        print(f"Media folder: {stems.media_folder_id}")
        print(f"Video: {stems.video.name} ({stems.video.id})")
        print(f"Audio: {stems.audio.name} ({stems.audio.id})")
        print(f"Upload target: {stems.output_parent_id}/{stems.output_name}")

        uploaded = combine_stems_media_to_drive(
            drive,
            pkg_link,
            work_dir=args.work_dir,
            ffmpeg_path=args.ffmpeg,
            force=args.force,
            dry_run=args.dry_run,
            **kwargs,
        )
    except DriveCombineError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("Dry run complete.")
        return 0

    print(f"Uploaded: {uploaded.name} ({uploaded.id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
