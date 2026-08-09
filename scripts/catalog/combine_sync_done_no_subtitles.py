"""Combine media for Sync-done catalog videos that have no Translation resources."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import (
    AirtableClient,
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    STATUS_SYNC_DONE,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import (
    check_mixable_media,
    format_mix_media_check,
    mix_folder_media_to_drive,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.config import load_workflow_config


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _json_quote(value: str) -> str:
    return json.dumps(value)


def _has_text(value: object) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        value = str(value)
    return bool(value.strip())


def _filter_formula() -> str:
    return (
        f"AND("
        f'FIND("Synchronization done", {{{FIELD_STATUS}}} & ""), '
        f"OR("
        f"{{{FIELD_TYPE}}} = {_json_quote(TYPE_VIDEO)}, "
        f"{{{FIELD_TYPE}}} = {_json_quote(TYPE_REEL)}"
        f"), "
        f"OR("
        f"{{{FIELD_TRANSLATION_RESOURCES}}} = BLANK(), "
        f'{{{FIELD_TRANSLATION_RESOURCES}}} = ""'
        f")"
        f")"
    )


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Generate Combined Media File for Synchronization done Video/Reel "
            "records that have no Translation resources, and write the Drive "
            "link back to Airtable."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets and validate mix inputs without uploading.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Also recombine records that already have Combined Media File.",
    )
    args = parser.parse_args()

    config = load_workflow_config(PROJECT_ROOT)
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )
    drive = get_drive_service_noninteractive()
    output_parent_id = extract_drive_folder_id(config.output_drive_folder)
    if output_parent_id is None:
        raise RuntimeError(
            f"Could not parse output Drive folder: {config.output_drive_folder!r}"
        )

    records = airtable.list_records(filter_formula=_filter_formula())
    targets: list[tuple[str, str, str, str | None]] = []
    for record in records:
        fields = record.get("fields", {})
        if not isinstance(fields, dict):
            continue
        if not args.include_existing and _has_text(fields.get(FIELD_COMBINED_MEDIA_FILE)):
            continue
        record_id = record.get("id")
        title = fields.get(FIELD_TITLE)
        drive_link = fields.get(FIELD_VIDEO_FOLDER)
        record_type = fields.get(FIELD_TYPE)
        if not isinstance(record_id, str):
            continue
        if not isinstance(title, str) or not title.strip():
            print(f"SKIP {record_id}: missing title")
            continue
        if not isinstance(drive_link, str) or not drive_link.strip():
            print(f"SKIP {title}: missing Video Folder")
            continue
        video_type = (
            record_type
            if isinstance(record_type, str) and record_type.strip()
            else None
        )
        targets.append((record_id, title, drive_link, video_type))

    print(
        f"Found {len(targets)} Synchronization done video(s) without subtitles"
        f"{'' if args.include_existing else ' and without Combined Media File'}"
        f" (status match {STATUS_SYNC_DONE!r})",
        flush=True,
    )
    if not targets:
        return 0

    failures = 0
    for index, (record_id, title, drive_link, video_type) in enumerate(
        targets, start=1
    ):
        print(f"[{index}/{len(targets)}] {title} ({video_type or 'unknown'})", flush=True)
        pkg_folder_id = extract_drive_folder_id(drive_link)
        if pkg_folder_id is None:
            print("  FAIL: invalid Video Folder link")
            failures += 1
            continue

        check = check_mixable_media(drive, pkg_folder_id, video_type=video_type)
        print(f"  {format_mix_media_check(check)}", flush=True)
        if not check.ok:
            print("  FAIL: not mixable")
            failures += 1
            continue

        output_name = title if title.casefold().endswith(".mp4") else f"{title}.mp4"
        work_dir = config.work_dir / record_id
        try:
            created = mix_folder_media_to_drive(
                drive,
                pkg_folder_id=pkg_folder_id,
                output_parent_id=output_parent_id,
                output_name=output_name,
                work_dir=work_dir,
                dry_run=args.dry_run,
                video_type=video_type,
            )
        except Exception as exc:
            print(f"  FAIL: {exc}")
            failures += 1
            continue

        if args.dry_run:
            print(f"  DRY-RUN: would upload {output_name!r} as {created.id}")
            continue

        drive_url = f"https://drive.google.com/file/d/{created.id}/view"
        airtable.update_record_fields(
            record_id,
            {FIELD_COMBINED_MEDIA_FILE: drive_url},
        )
        print(f"  OK: {drive_url}", flush=True)

    print()
    print(f"Done: {len(targets) - failures} succeeded, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
