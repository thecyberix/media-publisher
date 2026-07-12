"""One-off: check Editing done Reels/Videos for editor-done comments."""

from __future__ import annotations

import json
import os
import re
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
from catalog_parser.airtable import FIELD_TITLE, FIELD_TYPE
from catalog_parser.parser import TYPE_SHORT, TYPE_VIDEO, TYPE_REEL
from check_missing_description_comments import AirtableApi, FIELD_STATUS

STATUS_EDITING_DONE = "3. Editing done"
EDITOR_MARKERS = ("редактирано", "редактиран")
EDITOR_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(marker) for marker in EDITOR_MARKERS) + r")\b",
    re.IGNORECASE,
)
OUTPUT_PATH = PROJECT_ROOT / "_tmp_editing_done_editor_ready_comments.json"


def comment_indicates_editing_done(comments: list[dict[str, Any]]) -> tuple[bool, str | None]:
    for comment in comments:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        if EDITOR_PATTERN.search(text):
            return True, text.strip()
    return False, None


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")

    token = os.getenv("AIRTABLE_TOKEN", "").strip()
    base_id = os.getenv("AIRTABLE_BASE_ID", "").strip()
    table_name = os.getenv("AIRTABLE_TABLE_NAME", "").strip()
    if not token or not base_id or not table_name:
        print("Missing AIRTABLE_TOKEN, AIRTABLE_BASE_ID, or AIRTABLE_TABLE_NAME in .env")
        return 1

    api = AirtableApi(token, base_id, table_name)
    records = api.list_records(
        filter_formula=f"{{{FIELD_STATUS}}}='{STATUS_EDITING_DONE}'"
    )
    records = [
        record
        for record in records
        if record.get("fields", {}).get(FIELD_TYPE) in (TYPE_REEL, TYPE_VIDEO)
    ]
    print(
        f"Fetched {len(records)} Reel/Video record(s) in '{STATUS_EDITING_DONE}' "
        f"(Shorts skipped)"
    )

    has_editor_comment: list[dict[str, Any]] = []
    missing_editor_comment: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for record in records:
        record_id = record["id"]
        fields = record.get("fields", {})
        title = fields.get(FIELD_TITLE, "(no title)")
        entry = {
            "record_id": record_id,
            "title": title,
            "type": fields.get(FIELD_TYPE),
            "status": fields.get(FIELD_STATUS),
            "editor": fields.get("Editor"),
            "translator": fields.get("Translator"),
        }

        try:
            comments = api.list_comments(record_id)
        except RuntimeError as exc:
            errors.append({**entry, "error": str(exc)})
            continue

        entry["comment_count"] = len(comments)
        done, matching_comment = comment_indicates_editing_done(comments)
        if done:
            has_editor_comment.append({**entry, "matching_comment": matching_comment})
        else:
            missing_editor_comment.append(
                {
                    **entry,
                    "comment_previews": [
                        c.get("text", "")[:120]
                        for c in comments[:5]
                        if isinstance(c.get("text"), str)
                    ],
                }
            )

    result = {
        "status_checked": STATUS_EDITING_DONE,
        "types_checked": [TYPE_REEL, TYPE_VIDEO],
        "editor_markers": list(EDITOR_MARKERS),
        "summary": {
            "total_records": len(records),
            "has_editor_done_comment": len(has_editor_comment),
            "missing_editor_done_comment": len(missing_editor_comment),
            "errors": len(errors),
        },
        "missing_editor_done_comment": missing_editor_comment,
        "has_editor_done_comment": has_editor_comment,
        "errors": errors,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report to {OUTPUT_PATH}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if missing_editor_comment:
        print("\nMissing editor-done comment:")
        for item in missing_editor_comment:
            editor = item.get("editor") or "(no editor)"
            print(f"  - [{item.get('type')}] {item['title']} ({item['record_id']}) — {editor}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
