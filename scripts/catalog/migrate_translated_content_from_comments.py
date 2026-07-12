"""Copy translated/edited title and description comments into Airtable fields."""

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
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.workflow.comments import extract_translated_content_from_comments

OUTPUT_PATH = PROJECT_ROOT / "_tmp_migrate_translated_content_report.json"
TARGET_STATUSES = (STATUS_TODO, STATUS_TRANSLATION_DONE, STATUS_EDITING_DONE)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _field_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = value.strip()
    return normalized or None


def _status_filter() -> str:
    status_parts = [f"{{{FIELD_STATUS}}} = {json.dumps(status)}" for status in TARGET_STATUSES]
    return f"OR({', '.join(status_parts)})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write field updates to Airtable (default is dry-run).",
    )
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )

    records = airtable.list_records(filter_formula=_status_filter())
    report: dict[str, Any] = {
        "dry_run": not args.apply,
        "statuses": list(TARGET_STATUSES),
        "total_records": len(records),
        "with_title_comment": 0,
        "with_description_comment": 0,
        "to_update": [],
        "skipped_no_comment": 0,
        "skipped_already_set": 0,
        "updated": 0,
    }

    for record in sorted(
        records,
        key=lambda item: str((item.get("fields") or {}).get(FIELD_TITLE) or "").casefold(),
    ):
        record_id = record.get("id")
        fields = record.get("fields") or {}
        if not isinstance(record_id, str) or not record_id:
            continue

        title = _field_text(fields.get(FIELD_TITLE)) or record_id
        existing_name = _field_text(fields.get(FIELD_VIDEO_NAME_TRANSLATED))
        existing_description = _field_text(fields.get(FIELD_VIDEO_DESCRIPTION_TRANSLATED))

        comments = airtable.list_comments(record_id)
        extracted = extract_translated_content_from_comments(comments)
        update_fields: dict[str, str] = {}

        if extracted.video_name_translated:
            report["with_title_comment"] += 1
            if existing_name != extracted.video_name_translated:
                update_fields[FIELD_VIDEO_NAME_TRANSLATED] = extracted.video_name_translated

        if extracted.video_description_translated:
            report["with_description_comment"] += 1
            if existing_description != extracted.video_description_translated:
                update_fields[FIELD_VIDEO_DESCRIPTION_TRANSLATED] = (
                    extracted.video_description_translated
                )

        if not extracted.video_name_translated and not extracted.video_description_translated:
            report["skipped_no_comment"] += 1
            continue

        if not update_fields:
            report["skipped_already_set"] += 1
            continue

        item = {
            "record_id": record_id,
            "title": title,
            "status": fields.get(FIELD_STATUS),
            "fields": update_fields,
            "existing": {
                FIELD_VIDEO_NAME_TRANSLATED: existing_name,
                FIELD_VIDEO_DESCRIPTION_TRANSLATED: existing_description,
            },
        }
        report["to_update"].append(item)

        action = "WOULD UPDATE" if not args.apply else "UPDATING"
        field_names = ", ".join(update_fields)
        print(f"{action}: {title} ({record_id}) -> {field_names}")

        if args.apply:
            airtable.update_record_fields(record_id, update_fields)
            report["updated"] += 1

    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"Records scanned: {len(records)}")
    print(f"With translated title comment: {report['with_title_comment']}")
    print(f"With translated description comment: {report['with_description_comment']}")
    print(f"To update: {len(report['to_update'])}")
    print(f"Skipped (no matching comments): {report['skipped_no_comment']}")
    print(f"Skipped (fields already match): {report['skipped_already_set']}")
    if args.apply:
        print(f"Updated: {report['updated']}")
    else:
        print("Dry run only. Re-run with --apply to write changes.")
    print(f"Report: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
