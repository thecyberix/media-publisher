"""Copy Combined Media File entries to a new Drive folder and update Airtable links."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
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
    FIELD_TYPE,
    STATUS_EDITING_DONE,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import (
    drive_file_view_url,
    extract_drive_file_id,
    extract_drive_folder_id,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from googleapiclient.errors import HttpError

DEFAULT_TARGET_FOLDER = (
    "https://drive.google.com/drive/folders/1sE-DZV2lrRJxEK7Fnjw7uU8y0KXg7imd"
)
OUTPUT_PATH = PROJECT_ROOT / "_tmp_migrate_combined_media_report.json"


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _file_parents(drive: Any, file_id: str) -> set[str]:
    metadata = (
        drive.files()
        .get(fileId=file_id, fields="id,name,parents", supportsAllDrives=True)
        .execute()
    )
    parents = metadata.get("parents", [])
    if not isinstance(parents, list):
        return set()
    return {parent for parent in parents if isinstance(parent, str)}


def _copy_file_to_folder(
    drive: Any,
    *,
    source_file_id: str,
    target_folder_id: str,
    name: str | None,
) -> str:
    body: dict[str, Any] = {"parents": [target_folder_id]}
    if name:
        body["name"] = name
    created = (
        drive.files()
        .copy(
            fileId=source_file_id,
            body=body,
            fields="id,name",
            supportsAllDrives=True,
        )
        .execute()
    )
    file_id = created.get("id")
    if not isinstance(file_id, str) or not file_id:
        raise RuntimeError(f"Drive copy did not return a file id for source {source_file_id!r}")
    return file_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-folder",
        default=os.getenv("OUTPUT_DRIVE_FOLDER", DEFAULT_TARGET_FOLDER),
        help="Destination Drive folder URL or id.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan only; do not copy or update.")
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    target_folder_id = extract_drive_folder_id(args.target_folder)
    if target_folder_id is None:
        raise RuntimeError(f"Could not parse target folder id from {args.target_folder!r}")

    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )
    drive = get_drive_service_noninteractive()

    formula = (
        "AND({Status} = "
        + json.dumps(STATUS_EDITING_DONE)
        + ", OR({Type} = "
        + json.dumps(TYPE_REEL)
        + ", {Type} = "
        + json.dumps(TYPE_VIDEO)
        + "))"
    )
    records = airtable.list_records(filter_formula=formula)

    results: list[dict[str, Any]] = []
    failures = 0

    print(f"Target folder: {args.target_folder}")
    print(f"Editing done records: {len(records)}")

    for record in records:
        record_id = record.get("id")
        fields = record.get("fields", {})
        if not isinstance(record_id, str) or not isinstance(fields, dict):
            continue

        title = fields.get(FIELD_TITLE)
        combined = fields.get(FIELD_COMBINED_MEDIA_FILE)
        entry: dict[str, Any] = {
            "record_id": record_id,
            "title": title if isinstance(title, str) else None,
            "type": fields.get(FIELD_TYPE),
            "old_url": combined if isinstance(combined, str) else None,
        }

        if not isinstance(combined, str) or not combined.strip():
            entry["action"] = "skip_no_combined_media"
            results.append(entry)
            print(f"SKIP (no combined media): {title}")
            continue

        source_file_id = extract_drive_file_id(combined)
        if source_file_id is None:
            entry["action"] = "fail_invalid_url"
            failures += 1
            results.append(entry)
            print(f"FAIL invalid combined media URL: {title}")
            continue

        try:
            parents = _file_parents(drive, source_file_id)
        except HttpError as exc:
            entry["action"] = "fail_source_lookup"
            entry["error"] = str(exc)
            failures += 1
            results.append(entry)
            print(f"FAIL cannot read source file: {title} ({exc})")
            continue

        if target_folder_id in parents:
            new_url = drive_file_view_url(source_file_id)
            entry["action"] = "already_in_target"
            entry["new_url"] = new_url
            if new_url != combined.strip():
                if args.dry_run:
                    entry["airtable_update"] = "would_update_url"
                    print(f"DRY RUN update URL only: {title}")
                else:
                    airtable.update_record_fields(
                        record_id,
                        {FIELD_COMBINED_MEDIA_FILE: new_url},
                    )
                    entry["airtable_update"] = "updated_url"
                    print(f"OK update URL only: {title}")
            else:
                entry["airtable_update"] = "unchanged"
                print(f"OK already in target: {title}")
            results.append(entry)
            continue

        output_name = None
        if isinstance(title, str) and title.strip():
            output_name = title if title.casefold().endswith(".mp4") else f"{title}.mp4"

        if args.dry_run:
            entry["action"] = "would_copy"
            entry["source_file_id"] = source_file_id
            results.append(entry)
            print(f"DRY RUN copy: {title}")
            continue

        try:
            copied_id = _copy_file_to_folder(
                drive,
                source_file_id=source_file_id,
                target_folder_id=target_folder_id,
                name=output_name,
            )
        except HttpError as exc:
            entry["action"] = "fail_copy"
            entry["error"] = str(exc)
            failures += 1
            results.append(entry)
            print(f"FAIL copy: {title} ({exc})")
            continue

        new_url = drive_file_view_url(copied_id)
        airtable.update_record_fields(record_id, {FIELD_COMBINED_MEDIA_FILE: new_url})
        entry["action"] = "copied"
        entry["source_file_id"] = source_file_id
        entry["new_file_id"] = copied_id
        entry["new_url"] = new_url
        entry["airtable_update"] = "updated"
        results.append(entry)
        print(f"OK copied: {title}")
        print(f"  {new_url}")

    report = {
        "target_folder": args.target_folder,
        "target_folder_id": target_folder_id,
        "dry_run": args.dry_run,
        "total_records": len(records),
        "results": results,
        "failures": failures,
    }
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"Failures: {failures}")
    print(f"Report: {OUTPUT_PATH}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
