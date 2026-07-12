"""One-off: find Video/Reel rows where Drive YT title differs from Airtable title but no title comment."""

from __future__ import annotations

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
from catalog_parser.airtable import FIELD_TITLE, FIELD_TYPE, FIELD_VIDEO_FOLDER, normalize_title
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import DEFAULT_YT_TITLE_FIELD, enrich_records_with_yt_titles
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from check_missing_description_comments import (
    AirtableApi,
    FIELD_STATUS,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
    airtable_record_to_catalog,
)

YT_TITLE_COMMENT_PREFIX = "Заглавие:"
SHORT_TITLE_COMMENT_PREFIX = "Съкратено заглавие:"
TITLE_COMMENT_PREFIXES = (YT_TITLE_COMMENT_PREFIX, SHORT_TITLE_COMMENT_PREFIX)
OUTPUT_PATH = PROJECT_ROOT / "_tmp_missing_title_comments.json"


def comment_has_title(comments: list[dict[str, Any]]) -> bool:
    for comment in comments:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        stripped = text.strip()
        if any(stripped.startswith(prefix) for prefix in TITLE_COMMENT_PREFIXES):
            return True
    return False


def titles_differ(airtable_title: Any, yt_title: Any) -> bool:
    normalized_airtable = normalize_title(airtable_title)
    normalized_yt = normalize_title(yt_title)
    if normalized_yt is None:
        return False
    if normalized_airtable is None:
        return True
    return normalized_airtable != normalized_yt


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    api = AirtableApi(token, base_id, table_name)
    filter_formula = (
        f"AND(OR({{{FIELD_TYPE}}}='{TYPE_VIDEO}',{{{FIELD_TYPE}}}='{TYPE_REEL}'),"
        f"OR({{{FIELD_STATUS}}}='{STATUS_TODO}',"
        f"{{{FIELD_STATUS}}}='{STATUS_TRANSLATION_DONE}'))"
    )
    records = api.list_records(filter_formula=filter_formula)
    print(
        f"Fetched {len(records)} Video/Reel record(s) in "
        f"'{STATUS_TODO}' or '{STATUS_TRANSLATION_DONE}'"
    )

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

    missing: list[dict[str, Any]] = []
    same_title: list[dict[str, Any]] = []
    has_title_comment: list[dict[str, Any]] = []
    no_video_folder: list[dict[str, Any]] = []
    no_yt_title_in_drive: list[dict[str, Any]] = []
    comment_check_errors: list[dict[str, Any]] = []

    for record, catalog in zip(records, enriched, strict=True):
        record_id = record["id"]
        fields = record.get("fields", {})
        airtable_title = fields.get(FIELD_TITLE)
        yt_title = catalog.get(DEFAULT_YT_TITLE_FIELD)
        status = fields.get(FIELD_STATUS)
        record_type = fields.get(FIELD_TYPE)
        video_folder = fields.get(FIELD_VIDEO_FOLDER)

        entry = {
            "record_id": record_id,
            "status": status,
            "type": record_type,
            "airtable_title": airtable_title,
            "drive_yt_title": yt_title,
            "video_folder": video_folder,
        }

        if not video_folder:
            no_video_folder.append(entry)
            continue

        if normalize_title(yt_title) is None:
            no_yt_title_in_drive.append(entry)
            continue

        if not titles_differ(airtable_title, yt_title):
            same_title.append(entry)
            continue

        try:
            comments = api.list_comments(record_id)
        except RuntimeError as exc:
            comment_check_errors.append({**entry, "error": str(exc)})
            continue

        if comment_has_title(comments):
            has_title_comment.append({**entry, "comment_count": len(comments)})
        else:
            missing.append({**entry, "comment_count": len(comments)})

    result = {
        "statuses_checked": [STATUS_TODO, STATUS_TRANSLATION_DONE],
        "types_checked": [TYPE_VIDEO, TYPE_REEL],
        "title_comment_prefixes": list(TITLE_COMMENT_PREFIXES),
        "summary": {
            "total_checked": len(records),
            "titles_differ_missing_comment": len(missing),
            "titles_differ_has_comment": len(has_title_comment),
            "same_title": len(same_title),
            "no_yt_title_in_drive": len(no_yt_title_in_drive),
            "no_video_folder": len(no_video_folder),
            "comment_check_errors": len(comment_check_errors),
        },
        "missing_title_comment": missing,
        "titles_differ_has_comment": has_title_comment,
        "same_title": same_title,
        "no_yt_title_in_drive": no_yt_title_in_drive,
        "no_video_folder": no_video_folder,
        "comment_check_errors": comment_check_errors,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report to {OUTPUT_PATH}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if missing:
        print("\nMissing title comment (Drive YT title differs from Airtable):")
        for item in missing:
            print(f"  - [{item['status']}] [{item['type']}] {item['airtable_title']!r}")
            print(f"    Drive: {item['drive_yt_title']!r}")
            print(f"    ID: {item['record_id']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
