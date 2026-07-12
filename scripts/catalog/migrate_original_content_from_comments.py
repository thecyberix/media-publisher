"""Set Original Video Name for active workflow rows from YT title or Title."""

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
    FIELD_ORIGINAL_VIDEO_DESCRIPTION,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.workflow.comments import (
    YT_TITLE_COMMENT_PREFIX,
    _latest_comment_value_for_prefix,
    extract_original_content_from_comments,
)

OUTPUT_PATH = PROJECT_ROOT / "_tmp_migrate_original_content_report.json"
TARGET_STATUSES = (STATUS_TODO, STATUS_TRANSLATION_DONE)


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
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include every status (default limits to To do and Translation done).",
    )
    parser.add_argument(
        "--include-description",
        action="store_true",
        help="Also copy Описание: comments into Original Video Description.",
    )
    args = parser.parse_args()

    load_env_file(PROJECT_ROOT / ".env")
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )

    records = airtable.list_records(
        filter_formula=None if args.all_statuses else _status_filter(),
    )
    report: dict[str, Any] = {
        "dry_run": not args.apply,
        "statuses": list(TARGET_STATUSES) if not args.all_statuses else "all",
        "total_records": len(records),
        "with_title_comment": [],
        "with_title_fallback": [],
        "with_description_comment": [],
        "to_update": [],
        "skipped_no_source": 0,
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
        existing_name = _field_text(fields.get(FIELD_ORIGINAL_VIDEO_NAME))
        existing_description = _field_text(fields.get(FIELD_ORIGINAL_VIDEO_DESCRIPTION))

        comments = airtable.list_comments(record_id)
        yt_title_comment = _latest_comment_value_for_prefix(comments, YT_TITLE_COMMENT_PREFIX)
        extracted = extract_original_content_from_comments(
            comments,
            title_fallback=fields.get(FIELD_TITLE),
        )
        update_fields: dict[str, str] = {}

        if extracted.original_video_name:
            source = "yt_title_comment" if yt_title_comment else "title_fallback"
            bucket = (
                report["with_title_fallback"]
                if source == "title_fallback"
                else report["with_title_comment"]
            )
            bucket.append(
                {
                    "record_id": record_id,
                    "title": title,
                    "value": extracted.original_video_name,
                    "source": source,
                }
            )
            if existing_name != extracted.original_video_name:
                update_fields[FIELD_ORIGINAL_VIDEO_NAME] = extracted.original_video_name

        if args.include_description and extracted.original_video_description:
            report["with_description_comment"].append(
                {
                    "record_id": record_id,
                    "title": title,
                    "value": extracted.original_video_description,
                }
            )
            if existing_description != extracted.original_video_description:
                update_fields[FIELD_ORIGINAL_VIDEO_DESCRIPTION] = (
                    extracted.original_video_description
                )

        if not extracted.original_video_name and (
            not args.include_description or not extracted.original_video_description
        ):
            report["skipped_no_source"] = report.get("skipped_no_source", 0) + 1
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
                FIELD_ORIGINAL_VIDEO_NAME: existing_name,
                FIELD_ORIGINAL_VIDEO_DESCRIPTION: existing_description,
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
    print(f"With YT title (Заглавие:): {len(report['with_title_comment'])}")
    print(f"With Title fallback: {len(report['with_title_fallback'])}")
    if args.include_description:
        print(f"With Описание: comment: {len(report['with_description_comment'])}")
    print(f"To update: {len(report['to_update'])}")
    print(f"Skipped (no title source): {report['skipped_no_source']}")
    print(f"Skipped (fields already match): {report['skipped_already_set']}")
    if args.apply:
        print(f"Updated: {report['updated']}")
    else:
        print("Dry run only. Re-run with --apply to write changes.")
    print(f"Report: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
