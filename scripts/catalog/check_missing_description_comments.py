"""One-off: find Airtable rows with a Drive description but no Описание comment."""

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
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import (
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    YT_DESCRIPTION_COMMENT_PREFIX,
)
from catalog_parser.auth import get_docs_service, get_drive_service
from catalog_parser.drive_docs import (
    DEFAULT_YT_DESCRIPTION_FIELD,
    enrich_records_with_yt_titles,
)
from catalog_parser.parser import TYPE_REEL, TYPE_SHORT, TYPE_VIDEO, VIDEO_TYPES

STATUS_TODO = "1. To do"
STATUS_TRANSLATION_DONE = "2. Translation done"
FIELD_STATUS = "Status"
OUTPUT_PATH = PROJECT_ROOT / "_tmp_missing_description_comments.json"


class AirtableApi:
    def __init__(self, token: str, base_id: str, table_name: str) -> None:
        self.token = token
        self.base_id = base_id
        self.table_name = table_name
        self.table_url = (
            f"https://api.airtable.com/v0/{base_id}/"
            f"{urllib.parse.quote(table_name, safe='')}"
        )

    def _request(self, method: str, url: str, *, query: dict[str, str] | None = None) -> Any:
        if query:
            url = f"{url}?{urllib.parse.urlencode(query, doseq=True)}"
        request = urllib.request.Request(url, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def list_records(self, *, filter_formula: str | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            query: dict[str, str] = {}
            if filter_formula:
                query["filterByFormula"] = filter_formula
            if offset:
                query["offset"] = offset
            response = self._request("GET", self.table_url, query=query)
            records.extend(response.get("records", []))
            offset = response.get("offset")
            if not offset:
                break
        return records

    def list_comments(self, record_id: str) -> list[dict[str, Any]]:
        url = f"{self.table_url}/{urllib.parse.quote(record_id, safe='')}/comments"
        comments: list[dict[str, Any]] = []
        offset: str | None = None
        while True:
            query: dict[str, str] = {}
            if offset:
                query["offset"] = offset
            response = self._request("GET", url, query=query or None)
            comments.extend(response.get("comments", []))
            offset = response.get("offset")
            if not offset:
                break
        return comments


def comment_has_description(comments: list[dict[str, Any]]) -> bool:
    prefix = YT_DESCRIPTION_COMMENT_PREFIX
    for comment in comments:
        text = comment.get("text")
        if isinstance(text, str) and text.strip().startswith(prefix):
            return True
    return False


def has_drive_description(value: Any) -> bool:
    if value is None:
        return False
    if not isinstance(value, str):
        value = str(value)
    return bool(value.strip())


def airtable_record_to_catalog(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields", {})
    duration = fields.get("Duration")
    catalog: dict[str, Any] = {
        "ctTitle": fields.get(FIELD_TITLE),
        "ctDuration": duration,
        "pkgLink": fields.get(FIELD_VIDEO_FOLDER),
    }
    record_type = fields.get(FIELD_TYPE)
    if duration is None:
        if record_type == TYPE_REEL:
            catalog["ctDuration"] = 60
        elif record_type == TYPE_SHORT:
            catalog["ctDuration"] = 120
        elif record_type == TYPE_VIDEO:
            catalog["ctDuration"] = 200
    return catalog


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
        f"OR({{{FIELD_STATUS}}}='{STATUS_TODO}',"
        f"{{{FIELD_STATUS}}}='{STATUS_TRANSLATION_DONE}')"
    )
    records = api.list_records(filter_formula=filter_formula)
    print(f"Fetched {len(records)} record(s) in '{STATUS_TODO}' or '{STATUS_TRANSLATION_DONE}'")

    eligible: list[dict[str, Any]] = []
    skipped_type: list[dict[str, Any]] = []
    for record in records:
        record_type = record.get("fields", {}).get(FIELD_TYPE)
        if record_type not in VIDEO_TYPES:
            skipped_type.append(record)
            continue
        eligible.append(record)

    print(f"Records to check (Reel/Short/Video): {len(eligible)} (skipped {len(skipped_type)} other types)")

    drive_records = [airtable_record_to_catalog(record) for record in eligible]
    credentials_path = PROJECT_ROOT / "credentials.json"
    token_path = PROJECT_ROOT / "token.json"
    drive_service = get_drive_service(credentials_path, token_path)
    docs_service = get_docs_service(credentials_path, token_path)

    enriched = enrich_records_with_yt_titles(
        drive_records,
        drive_service,
        docs_service,
        folder_link_field="pkgLink",
        description_field=DEFAULT_YT_DESCRIPTION_FIELD,
    )

    comments_readable = True
    missing: list[dict[str, Any]] = []
    has_description_with_comment: list[dict[str, Any]] = []
    no_description_in_drive: list[dict[str, Any]] = []
    no_video_folder: list[dict[str, Any]] = []
    comment_check_errors: list[dict[str, Any]] = []

    for record, catalog in zip(eligible, enriched, strict=True):
        record_id = record["id"]
        fields = record.get("fields", {})
        title = fields.get(FIELD_TITLE, "(no title)")
        record_type = fields.get(FIELD_TYPE)
        status = fields.get(FIELD_STATUS)
        video_folder = fields.get(FIELD_VIDEO_FOLDER)

        yt_description = catalog.get(DEFAULT_YT_DESCRIPTION_FIELD)

        entry = {
            "record_id": record_id,
            "title": title,
            "status": status,
            "type": record_type,
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

        if not comments_readable:
            comment_check_errors.append({**entry, "error": "comments API not readable"})
            continue

        try:
            comments = api.list_comments(record_id)
        except RuntimeError as exc:
            if "HTTP 403" in str(exc):
                comments_readable = False
                comment_check_errors.append({**entry, "error": str(exc)})
                continue
            comment_check_errors.append({**entry, "error": str(exc)})
            continue

        if comment_has_description(comments):
            has_description_with_comment.append(entry)
        else:
            missing.append(
                {
                    **entry,
                    "expected_comment_prefix": YT_DESCRIPTION_COMMENT_PREFIX,
                    "comment_count": len(comments),
                }
            )

    result = {
        "statuses_checked": [STATUS_TODO, STATUS_TRANSLATION_DONE],
        "comments_readable": comments_readable,
        "summary": {
            "total_in_statuses": len(records),
            "checked_reel_short_video": len(eligible),
            "missing_description_comment": len(missing),
            "has_description_comment": len(has_description_with_comment),
            "no_description_in_drive": len(no_description_in_drive),
            "no_video_folder": len(no_video_folder),
            "comment_check_errors": len(comment_check_errors),
            "skipped_other_types": len(skipped_type),
        },
        "missing_description_comment": missing,
        "has_description_comment": has_description_with_comment,
        "no_description_in_drive": no_description_in_drive,
        "no_video_folder": no_video_folder,
        "comment_check_errors": comment_check_errors,
        "skipped_other_types": [
            {
                "record_id": r["id"],
                "title": r.get("fields", {}).get(FIELD_TITLE),
                "type": r.get("fields", {}).get(FIELD_TYPE),
                "status": r.get("fields", {}).get(FIELD_STATUS),
            }
            for r in skipped_type
        ],
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report to {OUTPUT_PATH}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if missing:
        print("\nMissing description comment:")
        for item in missing:
            print(f"  - [{item['status']}] {item['title']} ({item['record_id']})")

    if not comments_readable:
        print(
            "\nWARNING: Airtable token cannot read record comments "
            "(needs data.recordComments:read). Re-run after updating the token."
        )
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
