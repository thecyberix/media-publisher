"""One-off: add Описание comment to a single Airtable record by title."""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from add_missing_reel_description_comments import build_description_comment
from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import AirtableClient, FIELD_TITLE, FIELD_VIDEO_FOLDER
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    extract_drive_folder_id,
    read_drive_fields_from_folder,
)
from check_missing_description_comments import (
    AirtableApi,
    comment_has_description,
    has_drive_description,
)

TARGET_TITLE = "Truth Behind Allegations Against Sadhguru"


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    api_base = (
        os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0"
    )

    api = AirtableApi(token, base_id, table_name)
    client = AirtableClient(token, base_id, table_name, api_base=api_base)

    records = api.list_records(
        filter_formula=f"{{{FIELD_TITLE}}}='{TARGET_TITLE}'"
    )
    if not records:
        print(f"Record not found: {TARGET_TITLE!r}")
        return 1

    record = records[0]
    record_id = record["id"]
    fields = record["fields"]
    folder = fields.get(FIELD_VIDEO_FOLDER)

    comments = api.list_comments(record_id)
    if comment_has_description(comments):
        print(f"Already has description comment: {TARGET_TITLE}")
        return 0

    folder_id = extract_drive_folder_id(folder or "")
    if not folder_id:
        print("No Video Folder on record")
        return 1

    drive = get_drive_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    docs = get_docs_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    yt_description = read_drive_fields_from_folder(drive, docs, folder_id).get(
        DEFAULT_YT_DESCRIPTION_FIELD
    )
    if not has_drive_description(yt_description):
        print("No description found in Drive")
        return 1

    client.create_record_comment(record_id, build_description_comment(str(yt_description)))
    print(f"ADDED: {TARGET_TITLE} ({record_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
