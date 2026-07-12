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
from catalog_parser.airtable import FIELD_STATUS, FIELD_TITLE, FIELD_TYPE
from catalog_parser.workflow.comments import analyze_readiness
from check_missing_description_comments import AirtableApi

TYPE_VIDEO = "Video"
TYPE_REEL = "Reel"
STATUSES = (
    "1. To do",
    "2. Translation done",
    "3. Editing done",
    "5. Synchronization done",
)

# Interpreted expected readiness presence for current workflow stages.
EXPECTED = {
    "1. To do": {"translator_ready": False, "editor_ready": False},
    "2. Translation done": {"translator_ready": True, "editor_ready": False},
    "3. Editing done": {"translator_ready": True, "editor_ready": True},
    "5. Synchronization done": {"translator_ready": True, "editor_ready": True},
}


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
        "AND("
        + "OR(" + ",".join(f"{{{FIELD_STATUS}}}='{status}'" for status in STATUSES) + "),"
        + f"OR({{{FIELD_TYPE}}}='{TYPE_VIDEO}',{{{FIELD_TYPE}}}='{TYPE_REEL}')"
        + ")"
    )
    records = api.list_records(filter_formula=filter_formula)
    print(f"Fetched {len(records)} Video/Reel record(s) in target statuses")

    aligned: list[dict[str, Any]] = []
    mismatched: list[dict[str, Any]] = []
    summary_by_status: dict[str, dict[str, int]] = {
        status: {"total": 0, "aligned": 0, "mismatched": 0} for status in STATUSES
    }

    for record in records:
        record_id = record.get("id")
        fields = record.get("fields", {})
        if not isinstance(fields, dict) or not isinstance(record_id, str):
            continue
        status = fields.get(FIELD_STATUS)
        if status not in EXPECTED:
            continue
        summary_by_status[status]["total"] += 1
        comments = api.list_comments(record_id)
        readiness = analyze_readiness(comments)
        expected = EXPECTED[status]
        item = {
            "record_id": record_id,
            "title": fields.get(FIELD_TITLE),
            "type": fields.get(FIELD_TYPE),
            "status": status,
            "translator_ready": readiness.translator_ready,
            "editor_ready": readiness.editor_ready,
            "translator_comment": readiness.translator_comment,
            "editor_comment": readiness.editor_comment,
            "expected_translator_ready": expected["translator_ready"],
            "expected_editor_ready": expected["editor_ready"],
        }
        if (
            readiness.translator_ready == expected["translator_ready"]
            and readiness.editor_ready == expected["editor_ready"]
        ):
            aligned.append(item)
            summary_by_status[status]["aligned"] += 1
        else:
            mismatched.append(item)
            summary_by_status[status]["mismatched"] += 1

    out = {
        "note": "Audit uses current project readiness booleans, not exact numeric comment counts.",
        "expected_by_status": EXPECTED,
        "summary_by_status": summary_by_status,
        "aligned_count": len(aligned),
        "mismatched_count": len(mismatched),
        "mismatched": mismatched,
    }
    out_path = PROJECT_ROOT / "_tmp_status_readiness_audit.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}")
    print(json.dumps(summary_by_status, ensure_ascii=False, indent=2))
    if mismatched:
        print("\nMismatches:")
        for item in mismatched:
            print(
                f"- [{item['status']}] [{item['type']}] {item['title']} ({item['record_id']}) "
                f"expected T/E={item['expected_translator_ready']}/{item['expected_editor_ready']} "
                f"got {item['translator_ready']}/{item['editor_ready']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

