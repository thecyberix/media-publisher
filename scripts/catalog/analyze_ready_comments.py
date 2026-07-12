"""Analyze status-like comments on Editing done entries."""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(SCRIPTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import FIELD_TITLE, FIELD_TYPE
from check_missing_description_comments import AirtableApi, FIELD_STATUS

from catalog_parser.parser import TYPE_SHORT

STATUSES = (
    "1. To do",
    "2. Translation done",
    "3. Editing done",
)
EXCLUDED_STATUSES = ("5. Synchronization done",)
EXCLUDED_TYPES = (TYPE_SHORT,)
OUTPUT_PATH = PROJECT_ROOT / "_tmp_ready_comment_analysis.json"

# Prefixes that indicate content notes, not readiness status.
CONTENT_PREFIXES = (
    "заглавие:",
    "съкратено заглавие:",
    "описание:",
    "редактирано заглавие:",
    "редакция на заглавието:",
    "редакция на загланието:",
)


def normalize_comment(text: str) -> str:
    return " ".join(text.strip().split())


def is_content_note(text: str) -> bool:
    lowered = text.strip().casefold()
    return any(lowered.startswith(prefix) for prefix in CONTENT_PREFIXES)


def is_short_status_comment(text: str, max_len: int = 60) -> bool:
    text = normalize_comment(text)
    if not text:
        return False
    if is_content_note(text):
        return False
    if len(text) > max_len:
        return False
    if "\n" in text:
        return False
    return True


def classify_comment(text: str) -> str | None:
    lowered = normalize_comment(text).casefold()

    translator_patterns = [
        (r"^преводът е готов\.?$", "translator"),
        (r"^translation ready\.?$", "translator"),
        (r"^translated\.?$", "translator"),
        (r"^готово\.?$", "translator"),
        (r"^готов\.?$", "translator"),
        (r"^преведено\.?$", "translator"),
        (r"^преведен\.?$", "translator"),
        (r"^готова\.?$", "translator"),
        (r"^готови\.?$", "translator"),
    ]
    editor_patterns = [
        (r"^редактирано е\.?$", "editor"),
        (r"^редактирано\.?$", "editor"),
        (r"^редактиран\.?$", "editor"),
        (r"^редактирана\.?$", "editor"),
        (r"^editing done\.?$", "editor"),
        (r"^edited\.?$", "editor"),
    ]

    for pattern, kind in translator_patterns + editor_patterns:
        if re.fullmatch(pattern, lowered):
            return kind

    if re.search(r"\b(преведен|преведено|готов|готово|готова|готови)\b", lowered):
        if re.search(r"\b(редактиран|редактирано|редактирана)\b", lowered):
            return "mixed"
        if len(lowered) <= 60:
            return "translator_candidate"

    if re.search(r"\b(редактиран|редактирано|редактирана)\b", lowered):
        if len(lowered) <= 60:
            return "editor_candidate"

    return None


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    api = AirtableApi(
        os.environ["AIRTABLE_TOKEN"],
        os.environ["AIRTABLE_BASE_ID"],
        os.environ["AIRTABLE_TABLE_NAME"],
    )
    records: list[dict[str, Any]] = []
    for status in STATUSES:
        records.extend(
            api.list_records(filter_formula=f"{{{FIELD_STATUS}}}='{status}'")
        )
    records = [
        record
        for record in records
        if record.get("fields", {}).get(FIELD_TYPE) not in EXCLUDED_TYPES
    ]

    short_by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    all_short: Counter[str] = Counter()
    unclassified_short: Counter[str] = Counter()
    content_prefix_counts: Counter[str] = Counter()
    samples_with_author: list[dict[str, Any]] = []

    for record in records:
        fields = record.get("fields", {})
        comments = api.list_comments(record["id"])
        for comment in comments:
            text = comment.get("text")
            if not isinstance(text, str):
                continue
            stripped = normalize_comment(text)
            if not stripped:
                continue

            lowered = stripped.casefold()
            for prefix in CONTENT_PREFIXES:
                if lowered.startswith(prefix):
                    content_prefix_counts[prefix] += 1

            if not is_short_status_comment(stripped):
                continue

            key = stripped.casefold()
            all_short[key] += 1
            kind = classify_comment(stripped)
            if kind in ("translator", "editor"):
                short_by_kind[kind][key] += 1
            elif kind == "translator_candidate":
                short_by_kind["translator_other"][key] += 1
            elif kind == "editor_candidate":
                short_by_kind["editor_other"][key] += 1
            elif kind == "mixed":
                short_by_kind["mixed"][key] += 1
            else:
                unclassified_short[key] += 1

            if len(samples_with_author) < 5 and comment.get("author"):
                samples_with_author.append(
                    {
                        "text": stripped,
                        "author": comment.get("author"),
                        "record": fields.get(FIELD_TITLE),
                    }
                )

    def top(counter: Counter[str], n: int = 30) -> list[dict[str, Any]]:
        return [{"text": text, "count": count} for text, count in counter.most_common(n)]

    result = {
        "statuses_analyzed": list(STATUSES),
        "excluded_statuses": list(EXCLUDED_STATUSES),
        "excluded_types": list(EXCLUDED_TYPES),
        "records_analyzed": len(records),
        "translator_ready_exact": top(short_by_kind["translator"]),
        "editor_ready_exact": top(short_by_kind["editor"]),
        "translator_ready_other_short": top(short_by_kind["translator_other"]),
        "editor_ready_other_short": top(short_by_kind["editor_other"]),
        "mixed_short": top(short_by_kind["mixed"]),
        "unclassified_short_comments": top(unclassified_short, 50),
        "all_short_comments": top(all_short, 80),
        "content_note_prefixes": top(content_prefix_counts),
        "author_sample": samples_with_author,
    }

    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    for section in (
        "translator_ready_exact",
        "editor_ready_exact",
        "translator_ready_other_short",
        "editor_ready_other_short",
        "unclassified_short_comments",
    ):
        print(f"\n== {section} ==")
        for item in result[section][:20]:
            print(f"  {item['count']:>3}  {item['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
