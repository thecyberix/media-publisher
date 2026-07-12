"""Audit unpublished Airtable videos against SM catalog pkgTn marks."""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse, urlunparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src/catalog_parser"))

from catalog_parser.parser import rows_to_records
from google.oauth2 import service_account
from googleapiclient.discovery import build

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
)

FIELD_ORIGINAL_VIDEO_NAME = "Original Video Name"

SHEET_ID = "1BGxTfnvs3zezyJVTSXroy9N0l7j5QHbzPzRj_TSjO-c"
SHEET_TAB = "English"
TN_FIELD = "pkgTn"
STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)
ORIGINAL_VIDEO_NAME_SUFFIX = " | Sadhguru"


def normalize_title(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(ORIGINAL_VIDEO_NAME_SUFFIX):
        text = text[: -len(ORIGINAL_VIDEO_NAME_SUFFIX)].rstrip()
    collapsed = " ".join(text.casefold().split())
    return collapsed or None


def normalize_url(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = urlparse(text)
    if not parsed.scheme:
        return text.casefold()
    path = parsed.path.rstrip("/")
    netloc = parsed.netloc.casefold()
    return urlunparse((parsed.scheme.casefold(), netloc, path, "", parsed.query, ""))


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def tn_is_marked(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.upper() != "X"


def fetch_catalog_records() -> list[dict]:
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=SHEET_TAB)
        .execute()
        .get("values", [])
    )
    if not values:
        return []
    return rows_to_records(values[0], values[1:])


def index_catalog(records: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    by_url: dict[str, list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if not record.get("pkgSmLk"):
            continue
        url_key = normalize_url(record.get("ctLink"))
        if url_key:
            by_url[url_key].append(record)
        title_key = normalize_title(record.get("ctTitle"))
        if title_key:
            by_title[title_key].append(record)
    return by_url, by_title


def match_catalog_row(
    fields: dict,
    by_url: dict[str, list[dict]],
    by_title: dict[str, list[dict]],
) -> tuple[dict | None, str | None]:
    for candidate in (
        fields.get(FIELD_ORIGINAL_VIDEO),
        fields.get("Original Video"),
    ):
        url_key = normalize_url(candidate)
        if url_key and by_url.get(url_key):
            return by_url[url_key][0], "original_video_url"

    for candidate in (
        fields.get(FIELD_ORIGINAL_VIDEO_NAME),
        fields.get(FIELD_TITLE),
        fields.get("Title"),
    ):
        title_key = normalize_title(candidate)
        if title_key and by_title.get(title_key):
            return by_title[title_key][0], "title"

    return None, None


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    catalog = fetch_catalog_records()
    by_url, by_title = index_catalog(catalog)

    rows: list[dict] = []
    for record in airtable.list_records(filter_formula=build_filter_formula()):
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        sheet_row, match_method = match_catalog_row(fields, by_url, by_title)
        rows.append(
            {
                "title": str(
                    fields.get(FIELD_ORIGINAL_VIDEO_NAME)
                    or fields.get(FIELD_TITLE)
                    or "Untitled"
                ).strip(),
                "status": bucket,
                "sheet_row": sheet_row,
                "match_method": match_method,
            }
        )

    print("=== Unpublished videos vs SM catalog pkgTn ===")
    print(f"SM catalog tab: {SHEET_TAB}")
    print(f"TN column:      {TN_FIELD}")
    print(f"Unpublished:    {len(rows)}")
    print()

    summary: dict[str, Counter] = defaultdict(Counter)
    marked_examples: list[dict] = []
    unmarked_examples: list[dict] = []

    for row in rows:
        bucket = row["status"]
        summary[bucket]["total"] += 1
        sheet_row = row["sheet_row"]
        if sheet_row is None:
            summary[bucket]["no_catalog_match"] += 1
            continue
        summary[bucket]["matched"] += 1
        tn_value = sheet_row.get(TN_FIELD)
        if tn_is_marked(tn_value):
            summary[bucket]["tn_marked"] += 1
            if len(marked_examples) < 8:
                marked_examples.append({**row, "tn_value": tn_value})
        else:
            summary[bucket]["tn_not_marked"] += 1
            if len(unmarked_examples) < 8:
                unmarked_examples.append({**row, "tn_value": tn_value})

    overall = Counter()
    for bucket in STATUS_KEYS:
        stats = summary[bucket]
        if not stats.get("total"):
            continue
        total = stats["total"]
        matched = stats.get("matched", 0)
        marked = stats.get("tn_marked", 0)
        print(f"{bucket} ({total}):")
        print(f"  matched in SM catalog: {matched}")
        print(f"  pkgTn != X:            {marked} ({100 * marked / total:.0f}% of bucket)")
        print(f"  pkgTn is X/empty:      {stats.get('tn_not_marked', 0)}")
        print(f"  no catalog match:      {stats.get('no_catalog_match', 0)}")
        print()
        overall["total"] += total
        overall["matched"] += matched
        overall["tn_marked"] += marked
        overall["tn_not_marked"] += stats.get("tn_not_marked", 0)
        overall["no_catalog_match"] += stats.get("no_catalog_match", 0)

    total = overall["total"] or 1
    print("=== Overall ===")
    print(f"Unpublished videos:     {overall['total']}")
    print(f"Matched in catalog:     {overall['matched']}")
    print(f"pkgTn marked (!= X):    {overall['tn_marked']} ({100 * overall['tn_marked'] / total:.0f}%)")
    print(f"pkgTn X or empty:       {overall['tn_not_marked']}")
    print(f"No catalog match:       {overall['no_catalog_match']}")
    print()

    if marked_examples:
        print("=== Examples: pkgTn marked ===")
        for item in marked_examples:
            print(f"- {item['title']} [{item['status']}] -> {item['tn_value']!r}")
        print()

    if unmarked_examples:
        print("=== Examples: matched but pkgTn is X/empty ===")
        for item in unmarked_examples:
            print(f"- {item['title']} [{item['status']}] -> {item['tn_value']!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
