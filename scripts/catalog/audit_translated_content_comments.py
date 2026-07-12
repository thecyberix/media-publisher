"""Audit title/description template comments for active workflow records."""

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
    AirtableClient,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    STATUS_EDITING_DONE,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_SHORT, TYPE_VIDEO
from catalog_parser.workflow.comments import (
    EDITED_DESCRIPTION_PREFIX,
    EDITED_TITLE_PREFIX,
    TRANSLATED_DESCRIPTION_PREFIX,
    TRANSLATED_TITLE_PREFIX,
    extract_translated_content_from_comments,
    extract_value_after_prefix,
)

OUTPUT_PATH = PROJECT_ROOT / "_tmp_translated_content_comments_audit.json"

TARGET_STATUSES = (STATUS_TRANSLATION_DONE, STATUS_EDITING_DONE)
TARGET_TYPES = (TYPE_REEL, TYPE_VIDEO)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _status_filter() -> str:
    status_parts = [f"{{{FIELD_STATUS}}} = {json.dumps(status)}" for status in TARGET_STATUSES]
    type_parts = [f"{{{FIELD_TYPE}}} = {json.dumps(video_type)}" for video_type in TARGET_TYPES]
    return f"AND(OR({', '.join(status_parts)}), OR({', '.join(type_parts)}))"


def _find_prefix_source(
    comments: list[dict[str, Any]],
    *,
    primary_prefix: str,
    fallback_prefix: str,
) -> str | None:
    ordered = sorted(comments, key=lambda comment: str(comment.get("createdTime", "")))
    has_primary = False
    has_fallback = False
    for comment in ordered:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        if extract_value_after_prefix(text, primary_prefix):
            has_primary = True
        if extract_value_after_prefix(text, fallback_prefix):
            has_fallback = True
    if has_primary:
        return "edited"
    if has_fallback:
        return "translated"
    return None


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    airtable = AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=_require_env("AIRTABLE_BASE_ID"),
        table_name=_require_env("AIRTABLE_TABLE_NAME"),
    )

    records = airtable.list_records(filter_formula=_status_filter())
    results: list[dict[str, Any]] = []
    missing_title: list[dict[str, Any]] = []

    for record in sorted(
        records,
        key=lambda item: (
            str((item.get("fields") or {}).get(FIELD_STATUS) or ""),
            str((item.get("fields") or {}).get(FIELD_TYPE) or ""),
            str((item.get("fields") or {}).get(FIELD_TITLE) or "").casefold(),
        ),
    ):
        record_id = record.get("id")
        fields = record.get("fields")
        if not isinstance(record_id, str) or not isinstance(fields, dict):
            continue

        video_type = fields.get(FIELD_TYPE)
        if video_type == TYPE_SHORT:
            continue

        title = fields.get(FIELD_TITLE)
        status = fields.get(FIELD_STATUS)
        comments = airtable.list_comments(record_id)
        extracted = extract_translated_content_from_comments(comments)
        title_source = _find_prefix_source(
            comments,
            primary_prefix=EDITED_TITLE_PREFIX,
            fallback_prefix=TRANSLATED_TITLE_PREFIX,
        )
        description_source = _find_prefix_source(
            comments,
            primary_prefix=EDITED_DESCRIPTION_PREFIX,
            fallback_prefix=TRANSLATED_DESCRIPTION_PREFIX,
        )

        entry = {
            "record_id": record_id,
            "title": title if isinstance(title, str) else None,
            "status": status if isinstance(status, str) else None,
            "type": video_type if isinstance(video_type, str) else None,
            "has_title_comment": extracted.video_name_translated is not None,
            "has_description_comment": extracted.video_description_translated is not None,
            "title_source": title_source,
            "description_source": description_source,
            "video_name_translated": extracted.video_name_translated,
            "video_description_translated": extracted.video_description_translated,
        }
        results.append(entry)
        if not entry["has_title_comment"]:
            missing_title.append(entry)

    by_status: dict[str, dict[str, int]] = {}
    for entry in results:
        status = entry["status"] or "unknown"
        bucket = by_status.setdefault(
            status,
            {
                "total": 0,
                "title_ok": 0,
                "title_missing": 0,
                "description_present": 0,
                "description_missing": 0,
            },
        )
        bucket["total"] += 1
        if entry["has_title_comment"]:
            bucket["title_ok"] += 1
        else:
            bucket["title_missing"] += 1
        if entry["has_description_comment"]:
            bucket["description_present"] += 1
        else:
            bucket["description_missing"] += 1

    report = {
        "statuses": list(TARGET_STATUSES),
        "types": list(TARGET_TYPES),
        "rules": {
            "title": [
                EDITED_TITLE_PREFIX,
                TRANSLATED_TITLE_PREFIX,
            ],
            "description_optional": [
                EDITED_DESCRIPTION_PREFIX,
                TRANSLATED_DESCRIPTION_PREFIX,
            ],
        },
        "summary_by_status": by_status,
        "total_checked": len(results),
        "missing_title_count": len(missing_title),
        "missing_title": missing_title,
        "results": results,
    }
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Checked: {len(results)} Reel/Video record(s)")
    for status, counts in by_status.items():
        print(f"\n{status}:")
        print(f"  title present: {counts['title_ok']}/{counts['total']}")
        print(f"  title missing: {counts['title_missing']}")
        print(
            f"  description present (optional): {counts['description_present']}/{counts['total']}"
        )
        print(f"  description missing (optional): {counts['description_missing']}")

    if missing_title:
        print("\nMissing title comment:")
        for entry in missing_title:
            print(f"  - [{entry['status']}] [{entry['type']}] {entry['title']}")

    print(f"\nReport: {OUTPUT_PATH}")
    return 1 if missing_title else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
