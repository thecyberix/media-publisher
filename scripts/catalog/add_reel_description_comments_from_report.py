"""One-off: add missing Описание comments from a reel description check report."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from add_missing_reel_description_comments import build_description_comment
from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import AirtableClient
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    extract_drive_folder_id,
    read_drive_fields_from_folder,
)
from check_missing_description_comments import AirtableApi, comment_has_description, has_drive_description


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add missing Описание comments from a check report JSON."
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=str(PROJECT_ROOT / "_tmp_3_editing_done_reel_missing_descriptions.json"),
        help="Path to check report JSON",
    )
    args = parser.parse_args()

    report_path = Path(args.report)
    if not report_path.exists():
        print(f"Report not found: {report_path}")
        return 1

    report = json.loads(report_path.read_text(encoding="utf-8"))
    targets = report.get("missing_description_comment", [])
    if not targets:
        print("No target records in report")
        return 0

    load_env_file(PROJECT_ROOT / ".env")
    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    api_base = (
        os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0"
    )
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    api = AirtableApi(token, base_id, table_name)
    client = AirtableClient(token, base_id, table_name, api_base=api_base)
    drive_service = get_drive_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    docs_service = get_docs_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")

    added = 0
    skipped = 0
    for item in targets:
        record_id = item["record_id"]
        title = item.get("title", record_id)
        video_folder = item.get("video_folder")

        comments = api.list_comments(record_id)
        if comment_has_description(comments):
            print(f"SKIP (already has comment): {title}")
            skipped += 1
            continue

        folder_id = extract_drive_folder_id(video_folder or "")
        if not folder_id:
            print(f"SKIP (no folder): {title}")
            skipped += 1
            continue

        yt_description = read_drive_fields_from_folder(
            drive_service,
            docs_service,
            folder_id,
        ).get(DEFAULT_YT_DESCRIPTION_FIELD)
        if not has_drive_description(yt_description):
            print(f"SKIP (no Drive description): {title}")
            skipped += 1
            continue

        client.create_record_comment(record_id, build_description_comment(str(yt_description)))
        added += 1
        print(f"ADDED: {title} ({record_id})")

    print(f"\nDone: added {added}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
