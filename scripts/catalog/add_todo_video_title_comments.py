"""One-off: add missing Заглавие comments for Videos in To do."""

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
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import (
    AirtableClient,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    build_yt_title_comment,
    normalize_title,
)
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import DEFAULT_YT_TITLE_FIELD, enrich_records_with_yt_titles
from catalog_parser.parser import TYPE_VIDEO
from check_missing_description_comments import (
    AirtableApi,
    FIELD_STATUS,
    STATUS_TODO,
    airtable_record_to_catalog,
)
from check_missing_title_comments import comment_has_title


def resolve_title_comment_text(yt_title: object, airtable_title: object) -> str | None:
    """Match catalog_record_comments: prefer Drive YT title, else Airtable title."""
    comment_text = build_yt_title_comment(yt_title)
    if comment_text:
        return comment_text
    return build_yt_title_comment(airtable_title)


def main() -> int:
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

    filter_formula = (
        f"AND({{{FIELD_TYPE}}}='{TYPE_VIDEO}',"
        f"{{{FIELD_STATUS}}}='{STATUS_TODO}')"
    )
    records = api.list_records(filter_formula=filter_formula)
    print(f"Found {len(records)} Video(s) in '{STATUS_TODO}'")

    drive_records = [airtable_record_to_catalog(record) for record in records]
    drive_service = get_drive_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    docs_service = get_docs_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    enriched = enrich_records_with_yt_titles(
        drive_records,
        drive_service,
        docs_service,
        folder_link_field="pkgLink",
        title_field=DEFAULT_YT_TITLE_FIELD,
    )

    added = 0
    skipped = 0
    for record, catalog in zip(records, enriched, strict=True):
        record_id = record["id"]
        fields = record.get("fields", {})
        airtable_title = fields.get(FIELD_TITLE, "(no title)")
        yt_title = catalog.get(DEFAULT_YT_TITLE_FIELD)

        if not fields.get(FIELD_VIDEO_FOLDER):
            print(f"SKIP (no Video Folder): {airtable_title}")
            skipped += 1
            continue

        comments = api.list_comments(record_id)
        if comment_has_title(comments):
            print(f"SKIP (already has title comment): {airtable_title}")
            skipped += 1
            continue

        comment_text = resolve_title_comment_text(yt_title, airtable_title)
        if not comment_text:
            print(f"SKIP (no title available): {airtable_title}")
            skipped += 1
            continue

        source = "Drive YT title" if normalize_title(yt_title) else "Airtable title"
        client.create_record_comment(record_id, comment_text)
        added += 1
        print(f"ADDED ({source}): {airtable_title}")
        print(f"  -> {comment_text.splitlines()[0]}")
        if "\n" in comment_text:
            print(f"     {comment_text.splitlines()[1]!r}")

    print(f"\nDone: added {added}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
