from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import (
    check_mixable_media,
    format_mix_media_check,
    mix_folder_media_to_drive,
    upload_package_video_to_drive,
)
from catalog_parser.__main__ import load_env_file


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _sanitize_mp4_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return "output.mp4"
    if not name.casefold().endswith(".mp4"):
        return f"{name}.mp4"
    return name


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Download a Drive video + all audio files in its single subfolder, "
            "mix audios together, mux into MP4, and upload to another Drive folder. "
            "Drive source link + output name are read from Airtable."
        )
    )
    record_group = parser.add_mutually_exclusive_group(required=True)
    record_group.add_argument("--record-id", help="Airtable record id, e.g. recXXXXXXXXXXXXXX")
    record_group.add_argument(
        "--title",
        help=f"Exact match for Airtable field {FIELD_TITLE!r} to find the record.",
    )
    parser.add_argument(
        "--output-drive-folder",
        default=os.getenv("OUTPUT_DRIVE_FOLDER", "").strip() or None,
        help="Target Drive folder link or id (or set OUTPUT_DRIVE_FOLDER).",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=PROJECT_ROOT / "_tmp_drive_mix",
        help="Local work directory for downloads and ffmpeg output.",
    )
    parser.add_argument(
        "--ffmpeg",
        default=os.getenv("FFMPEG_PATH", "").strip() or None,
        help="Optional path to ffmpeg binary (or set FFMPEG_PATH).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not download/upload, just validate inputs.")
    args = parser.parse_args()

    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )

    record_id = args.record_id
    if not record_id:
        record_id = airtable.find_record_id_by_exact_field(
            field_name=FIELD_TITLE,
            value=args.title,
        )
        if not record_id:
            # Allow truncated titles from chat/logs (prefix / substring match).
            escaped = args.title.replace('"', '\\"')
            formula = f'FIND("{escaped}", {{{FIELD_TITLE}}}) > 0'
            matches = airtable.list_records(filter_formula=formula)
            if len(matches) == 1:
                record_id = matches[0].get("id")
                if not isinstance(record_id, str) or not record_id:
                    record_id = None
            elif len(matches) > 1:
                titles = []
                for item in matches[:8]:
                    fields = item.get("fields", {})
                    title = fields.get(FIELD_TITLE) if isinstance(fields, dict) else None
                    titles.append(title if isinstance(title, str) else item.get("id"))
                raise AirtableError(
                    f"Multiple Airtable records match title containing {args.title!r}: "
                    + "; ".join(str(t) for t in titles)
                    + ". Use --record-id instead."
                )
        if not record_id:
            raise AirtableError(f"No Airtable record found with {FIELD_TITLE!r}={args.title!r}")

    record = airtable.get_record(record_id)
    fields = record.get("fields", {})
    if not isinstance(fields, dict):
        raise AirtableError("Airtable record is missing fields")

    drive_link = fields.get(FIELD_VIDEO_FOLDER)
    if not isinstance(drive_link, str) or not drive_link.strip():
        raise AirtableError(f"Airtable field {FIELD_VIDEO_FOLDER!r} is missing or empty")
    pkg_folder_id = extract_drive_folder_id(drive_link)
    if pkg_folder_id is None:
        raise AirtableError(f"Could not parse Drive folder id from {drive_link!r}")

    title = fields.get(FIELD_TITLE)
    if not isinstance(title, str) or not title.strip():
        raise AirtableError(f"Airtable field {FIELD_TITLE!r} is missing or empty")
    print(f"Record: {record_id}")
    print(f"Title: {title}")
    output_name = _sanitize_mp4_name(title)

    output_drive_folder = args.output_drive_folder
    if not output_drive_folder:
        raise RuntimeError("Provide --output-drive-folder or set OUTPUT_DRIVE_FOLDER")
    output_parent_id = extract_drive_folder_id(output_drive_folder)
    if output_parent_id is None:
        raise RuntimeError(f"Could not parse output Drive folder id from {output_drive_folder!r}")

    drive = get_drive_service_noninteractive()
    record_type = fields.get(FIELD_TYPE)
    video_type = record_type if isinstance(record_type, str) and record_type.strip() else None
    check = check_mixable_media(drive, pkg_folder_id, video_type=video_type)
    print(format_mix_media_check(check))
    if check.ok:
        created = mix_folder_media_to_drive(
            drive,
            pkg_folder_id=pkg_folder_id,
            output_parent_id=output_parent_id,
            output_name=output_name,
            work_dir=args.work_dir,
            ffmpeg_path=args.ffmpeg,
            dry_run=args.dry_run,
            video_type=video_type,
        )
        source_label = "mixed"
    elif check.error and "No audio files found" in check.error:
        print("No stem audio — uploading package video as Combined Media File")
        created = upload_package_video_to_drive(
            drive,
            pkg_folder_id=pkg_folder_id,
            output_parent_id=output_parent_id,
            output_name=output_name,
            work_dir=args.work_dir,
            dry_run=args.dry_run,
            video_type=video_type,
        )
        source_label = "video-only"
    else:
        raise RuntimeError(check.error or "Folder is not mixable")
    local_path = (args.work_dir / output_name).resolve()
    if args.dry_run:
        print(
            f"Dry run: would upload {output_name!r} to folder {output_parent_id!r} "
            f"({source_label})"
        )
        print(f"Local output path would be: {local_path}")
        return 0

    print(f"Local file: {local_path}")
    drive_url = f"https://drive.google.com/file/d/{created.id}/view"
    print(f"Uploaded ({source_label}): {created.name} (id={created.id})")
    print(f"URL: {drive_url}")

    airtable.update_record_fields(
        record_id,
        {FIELD_COMBINED_MEDIA_FILE: drive_url},
    )
    print(f"Airtable: updated {FIELD_COMBINED_MEDIA_FILE!r}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

