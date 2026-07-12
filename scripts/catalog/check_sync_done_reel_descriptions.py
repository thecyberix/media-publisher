"""One-off: check missing Описание comments for Reels in a given Airtable status."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import FIELD_TITLE, FIELD_TYPE, FIELD_VIDEO_FOLDER, YT_DESCRIPTION_COMMENT_PREFIX
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import DEFAULT_YT_DESCRIPTION_FIELD, enrich_records_with_yt_titles
from catalog_parser.parser import TYPE_REEL
from check_missing_description_comments import (
    AirtableApi,
    FIELD_STATUS,
    airtable_record_to_catalog,
    comment_has_description,
    has_drive_description,
)

STATUS_SYNC_DONE = "5. Synchronization done"
STATUS_EDITING_DONE = "3. Editing done"


def run_check(status: str, output_path: Path) -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    api = AirtableApi(token, base_id, table_name)
    filter_formula = (
        f"AND({{{FIELD_TYPE}}}='{TYPE_REEL}',"
        f"{{{FIELD_STATUS}}}='{status}')"
    )
    records = api.list_records(filter_formula=filter_formula)
    print(f"Fetched {len(records)} Reel(s) in '{status}'")

    drive_records = [airtable_record_to_catalog(record) for record in records]
    drive_service = get_drive_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    docs_service = get_docs_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    enriched = enrich_records_with_yt_titles(
        drive_records,
        drive_service,
        docs_service,
        folder_link_field="pkgLink",
        description_field=DEFAULT_YT_DESCRIPTION_FIELD,
    )

    missing: list[dict[str, Any]] = []
    has_description_with_comment: list[dict[str, Any]] = []
    no_description_in_drive: list[dict[str, Any]] = []
    no_video_folder: list[dict[str, Any]] = []

    for record, catalog in zip(records, enriched, strict=True):
        record_id = record["id"]
        fields = record.get("fields", {})
        title = fields.get(FIELD_TITLE, "(no title)")
        yt_description = catalog.get(DEFAULT_YT_DESCRIPTION_FIELD)
        video_folder = fields.get(FIELD_VIDEO_FOLDER)

        entry = {
            "record_id": record_id,
            "title": title,
            "status": fields.get(FIELD_STATUS),
            "type": fields.get(FIELD_TYPE),
            "video_folder": video_folder,
            "yt_description_preview": (
                yt_description[:120] + "..."
                if isinstance(yt_description, str) and len(yt_description) > 120
                else yt_description
            ),
        }

        if not video_folder:
            no_video_folder.append(entry)
            continue

        if not has_drive_description(yt_description):
            no_description_in_drive.append(entry)
            continue

        comments = api.list_comments(record_id)
        if comment_has_description(comments):
            has_description_with_comment.append(entry)
        else:
            missing.append({**entry, "comment_count": len(comments)})

    result = {
        "status_checked": status,
        "type_checked": TYPE_REEL,
        "summary": {
            "total_reels": len(records),
            "missing_description_comment": len(missing),
            "has_description_comment": len(has_description_with_comment),
            "no_description_in_drive": len(no_description_in_drive),
            "no_video_folder": len(no_video_folder),
        },
        "missing_description_comment": missing,
        "has_description_comment": has_description_with_comment,
        "no_description_in_drive": no_description_in_drive,
        "no_video_folder": no_video_folder,
    }

    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report to {output_path}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if missing:
        print(f"\nMissing {YT_DESCRIPTION_COMMENT_PREFIX} comment:")
        for item in missing:
            print(f"  - {item['title']} ({item['record_id']})")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check missing Описание comments for Reels in an Airtable status."
    )
    parser.add_argument(
        "--status",
        default=STATUS_SYNC_DONE,
        help=f"Airtable Status value (default: {STATUS_SYNC_DONE!r})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default: _tmp_<status>_reel_missing_descriptions.json)",
    )
    args = parser.parse_args()

    slug = args.status.lower().replace(".", "").replace(" ", "_")
    output_path = (
        Path(args.output)
        if args.output
        else PROJECT_ROOT / f"_tmp_{slug}_reel_missing_descriptions.json"
    )
    return run_check(args.status, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
