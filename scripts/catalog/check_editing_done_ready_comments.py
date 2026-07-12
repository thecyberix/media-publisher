"""One-off: check Editing done entries for translator-ready comments."""

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
from check_missing_description_comments import AirtableApi, FIELD_STATUS

STATUS_EDITING_DONE = "3. Editing done"
READY_MARKERS = ("готов", "готово", "преведен", "преведено")
READY_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(marker) for marker in READY_MARKERS) + r")\b",
    re.IGNORECASE,
)
OUTPUT_PATH = PROJECT_ROOT / "_tmp_editing_done_translation_ready_comments.json"


def comment_indicates_ready(comments: list[dict[str, Any]]) -> tuple[bool, str | None]:
    for comment in comments:
        text = comment.get("text")
        if not isinstance(text, str):
            continue
        if READY_PATTERN.search(text):
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
    print(f"Fetched {len(records)} record(s) in '{STATUS_EDITING_DONE}'")

    has_ready_comment: list[dict[str, Any]] = []
    missing_ready_comment: list[dict[str, Any]] = []
    no_comments: list[dict[str, Any]] = []
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
            "translator": fields.get("Translator"),
        }

        try:
            comments = api.list_comments(record_id)
        except RuntimeError as exc:
            errors.append({**entry, "error": str(exc)})
            continue

        entry["comment_count"] = len(comments)
        ready, matching_comment = comment_indicates_ready(comments)
        if ready:
            has_ready_comment.append({**entry, "matching_comment": matching_comment})
        elif not comments:
            no_comments.append(entry)
            missing_ready_comment.append(entry)
        else:
            missing_ready_comment.append(
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
        "ready_markers": list(READY_MARKERS),
        "summary": {
            "total_records": len(records),
            "has_ready_comment": len(has_ready_comment),
            "missing_ready_comment": len(missing_ready_comment),
            "no_comments_at_all": len(no_comments),
            "errors": len(errors),
        },
        "missing_ready_comment": missing_ready_comment,
        "has_ready_comment": has_ready_comment,
        "errors": errors,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report to {OUTPUT_PATH}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))

    if missing_ready_comment:
        print("\nMissing translation-ready comment:")
        for item in missing_ready_comment:
            translator = item.get("translator") or "(no translator)"
            print(f"  - [{item.get('type')}] {item['title']} ({item['record_id']}) — {translator}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
