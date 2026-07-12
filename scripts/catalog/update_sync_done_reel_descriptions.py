"""One-off: set Video description translated from Drive for 9 sync-done Reels."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
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
from catalog_parser.airtable import FIELD_TITLE, FIELD_VIDEO_FOLDER
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    extract_drive_folder_id,
    read_drive_fields_from_folder,
)
from check_missing_description_comments import AirtableApi, has_drive_description

FIELD_VIDEO_DESCRIPTION_TRANSLATED = "Video description translated"
REPORT_PATH = PROJECT_ROOT / "_tmp_sync_done_reel_missing_descriptions.json"


def patch_record_fields(
    api: AirtableApi,
    record_id: str,
    fields: dict[str, Any],
) -> None:
    url = f"{api.table_url}/{urllib.parse.quote(record_id, safe='')}"
    body = json.dumps({"fields": fields}).encode("utf-8")
    request = urllib.request.Request(url, data=body, method="PATCH")
    request.add_header("Authorization", f"Bearer {api.token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"PATCH {url} -> HTTP {exc.code}: {detail}") from exc


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    if not REPORT_PATH.exists():
        print(f"Report not found: {REPORT_PATH}")
        return 1

    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    targets = report.get("missing_description_comment", [])
    if not targets:
        print("No target records in report")
        return 0

    api = AirtableApi(token, base_id, table_name)
    drive_service = get_drive_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")
    docs_service = get_docs_service(PROJECT_ROOT / "credentials.json", PROJECT_ROOT / "token.json")

    updated = 0
    skipped = 0
    for item in targets:
        record_id = item["record_id"]
        title = item.get("title", record_id)
        video_folder = item.get("video_folder")

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

        patch_record_fields(
            api,
            record_id,
            {FIELD_VIDEO_DESCRIPTION_TRANSLATED: str(yt_description).strip()},
        )
        updated += 1
        print(f"UPDATED: {title} ({record_id})")

    print(f"\nDone: updated {updated}, skipped {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
