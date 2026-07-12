"""Check Drive folder structure for media combining on active workflow records."""

from __future__ import annotations

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
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
    AirtableClient,
)
from catalog_parser.auth import get_drive_service_noninteractive
from catalog_parser.drive_docs import extract_drive_folder_id
from catalog_parser.drive_mix import check_mixable_media, format_mix_media_check
from catalog_parser.parser import TYPE_REEL, TYPE_SHORT, TYPE_VIDEO

OUTPUT_PATH = PROJECT_ROOT / "_tmp_mixable_media_check.json"

TARGET_STATUSES = (STATUS_TODO, STATUS_TRANSLATION_DONE, STATUS_EDITING_DONE)
TARGET_TYPES = (TYPE_REEL, TYPE_VIDEO)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _status_filter() -> str:
    parts = [f"{{{FIELD_STATUS}}} = {json.dumps(status)}" for status in TARGET_STATUSES]
    return f"OR({', '.join(parts)})"


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
        api_base=os.getenv("AIRTABLE_API_BASE", "https://api.airtable.com/v0").strip()
        or "https://api.airtable.com/v0",
    )
    drive = get_drive_service_noninteractive()

    records = airtable.list_records(filter_formula=_status_filter())
    results: list[dict[str, Any]] = []
    ok_count = 0
    fail_count = 0
    skipped_count = 0

    for record in sorted(
        records,
        key=lambda item: (
            str((item.get("fields") or {}).get(FIELD_STATUS) or ""),
            str((item.get("fields") or {}).get(FIELD_TITLE) or "").casefold(),
        ),
    ):
        record_id = record.get("id")
        fields = record.get("fields")
        if not isinstance(record_id, str) or not isinstance(fields, dict):
            continue

        title = fields.get(FIELD_TITLE)
        status = fields.get(FIELD_STATUS)
        video_type = fields.get(FIELD_TYPE)
        drive_link = fields.get(FIELD_VIDEO_FOLDER)

        if video_type == TYPE_SHORT:
            skipped_count += 1
            continue
        if video_type not in TARGET_TYPES:
            skipped_count += 1
            continue

        entry: dict[str, Any] = {
            "record_id": record_id,
            "title": title if isinstance(title, str) else None,
            "status": status if isinstance(status, str) else None,
            "type": video_type if isinstance(video_type, str) else None,
            "video_folder": drive_link if isinstance(drive_link, str) else None,
        }

        if not isinstance(drive_link, str) or not drive_link.strip():
            entry["ok"] = False
            entry["error"] = "Missing Video Folder"
            fail_count += 1
            results.append(entry)
            print(f"FAIL [{status}] {title}: missing Video Folder")
            continue

        folder_id = extract_drive_folder_id(drive_link)
        if folder_id is None:
            entry["ok"] = False
            entry["error"] = "Could not parse Drive folder id"
            fail_count += 1
            results.append(entry)
            print(f"FAIL [{status}] {title}: invalid Video Folder link")
            continue

        check = check_mixable_media(drive, folder_id)
        entry["ok"] = check.ok
        entry["summary"] = format_mix_media_check(check)
        if check.ok:
            ok_count += 1
            media = check.media
            if media is not None:
                entry["video"] = media.video.name
                entry["audios"] = [audio.name for audio in media.audios]
            print(f"OK   [{status}] {title}: {entry['summary']}")
        else:
            entry["error"] = check.error
            fail_count += 1
            print(f"FAIL [{status}] {title}: {check.error}")

        results.append(entry)

    report = {
        "statuses": list(TARGET_STATUSES),
        "types": list(TARGET_TYPES),
        "total_checked": len(results),
        "ok": ok_count,
        "failed": fail_count,
        "skipped_short_or_other_type": skipped_count,
        "results": results,
    }
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"Checked: {len(results)} (ok={ok_count}, failed={fail_count}, skipped={skipped_count})")
    print(f"Report: {OUTPUT_PATH}")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
