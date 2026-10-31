"""Backfill the SM catalog's language PubDate column from already-published videos.

Sources: the live Airtable table plus the yearly archive bases (found through
the ``7. Archived`` pointer rows). Dry run by default; pass ``--apply`` to write.

Airtable usage is kept small: one filtered, field-limited listing per table,
plus a one-off schema lookup for the archives. Everything is cached under
``_tmp_sm_backfill/`` so repeat runs make no Airtable calls unless ``--refresh``
is given. The sheet is read with three requests and written in one batch.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.airtable import (
    FIELD_STATUS as ARCHIVE_FIELD_STATUS,
    FIELD_TITLE as ARCHIVE_FIELD_TITLE,
    STATUS_ARCHIVED,
    AirtableClient as MetaAirtableClient,
)
from catalog_parser.workflow.archive_sources import archive_pointers_from_records
from catalog_parser.workflow.config import load_catalog_id
from media_publisher.config import load_settings
from media_publisher.languages import selected_language
from media_publisher.sources.airtable import (
    DEFAULT_PUBLISH_TIMEZONE,
    FIELD_ORIGINAL_VIDEO,
    FIELD_SG_YT_DATE,
    FIELD_SG_YT_PUBLISHED,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    TYPE_QUOTE,
    AirtableClient,
    _parse_publish_at,
)
from media_publisher.sources.google_sheets import GoogleSheetsClient
from media_publisher.sources.sm_catalog import apply_plan, plan_publish_dates

CACHE_DIR = PROJECT_ROOT / "_tmp_sm_backfill"
CURRENT_CACHE = CACHE_DIR / "airtable_yt_published.json"
ARCHIVE_SOURCES_CACHE = CACHE_DIR / "archive_sources.json"
ARCHIVE_ROWS_CACHE = CACHE_DIR / "archive_yt_published.json"

WANTED_FIELDS = (FIELD_ORIGINAL_VIDEO, FIELD_SG_YT_DATE, FIELD_STATUS, FIELD_TITLE)
FILTER = (
    f"AND({{{FIELD_SG_YT_PUBLISHED}}}, {{{FIELD_SG_YT_DATE}}}, "
    f'{{{FIELD_TYPE}}} != "{TYPE_QUOTE}")'
)
# Archive tables are older and may not have every column; the filter only
# references fields that the schema lookup confirmed.
ARCHIVE_FILTER = f"AND({{{FIELD_SG_YT_PUBLISHED}}}, {{{FIELD_SG_YT_DATE}}})"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_current_rows(settings, *, refresh: bool) -> list[dict]:
    if CURRENT_CACHE.is_file() and not refresh:
        print(f"Airtable current table: cache (0 API calls)")
        return _read_json(CURRENT_CACHE)
    client = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    fields = [FIELD_ORIGINAL_VIDEO, FIELD_SG_YT_DATE, FIELD_STATUS, FIELD_TITLE]
    records = client.list_records(filter_formula=FILTER, fields=fields)
    print(
        f"Airtable current table: {len(records)} record(s), "
        f"{client.request_count} API call(s)"
    )
    rows = [{"id": record.id, **record.fields} for record in records]
    _write_json(CURRENT_CACHE, rows)
    return rows


def resolve_archive_tables(client: MetaAirtableClient, *, refresh: bool) -> list[dict]:
    """Archive base/table pairs (with their field names), found via pointer rows."""
    if ARCHIVE_SOURCES_CACHE.is_file() and not refresh:
        return _read_json(ARCHIVE_SOURCES_CACHE)
    pointer_rows = client.list_records(
        filter_formula=f'{{{ARCHIVE_FIELD_STATUS}}}="{STATUS_ARCHIVED}"',
        fields=[ARCHIVE_FIELD_STATUS, ARCHIVE_FIELD_TITLE],
    )
    pointers = archive_pointers_from_records(pointer_rows)
    if not pointers:
        return []
    bases = client.list_accessible_bases()
    sources: list[dict] = []
    for year, _invite in pointers:
        needle = f"archive {year}".casefold()
        base = next(
            (b for b in bases if needle in str(b.get("name") or "").casefold()), None
        )
        if base is None:
            print(f"  no accessible base for the {year} archive")
            continue
        for table in client.list_base_tables(str(base["id"])):
            names = [str(f.get("name")) for f in table.get("fields", [])]
            if FIELD_ORIGINAL_VIDEO in names and FIELD_SG_YT_DATE in names:
                sources.append(
                    {
                        "year": year,
                        "base_id": str(base["id"]),
                        "table_name": str(table["name"]),
                        "fields": names,
                    }
                )
    _write_json(ARCHIVE_SOURCES_CACHE, sources)
    return sources


def load_archive_rows(settings, *, refresh: bool) -> list[dict]:
    if ARCHIVE_ROWS_CACHE.is_file() and not refresh:
        print("Airtable archives: cache (0 API calls)")
        return _read_json(ARCHIVE_ROWS_CACHE)
    client = MetaAirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    sources = resolve_archive_tables(client, refresh=refresh)
    rows: list[dict] = []
    for source in sources:
        available = set(source["fields"])
        fields = [name for name in WANTED_FIELDS if name in available]
        formula = (
            ARCHIVE_FILTER
            if FIELD_SG_YT_PUBLISHED in available
            else f"{{{FIELD_SG_YT_DATE}}}"
        )
        records = client.list_records(
            filter_formula=formula,
            base_id=source["base_id"],
            table_name=source["table_name"],
            fields=fields,
        )
        print(f"  {source['year']} archive: {len(records)} record(s)")
        for record in records:
            rows.append(
                {
                    "id": record.get("id"),
                    "_source": f"{source['year']} archive",
                    **(record.get("fields") or {}),
                }
            )
    print(f"Airtable archives: {len(rows)} record(s), {client.request_count} API call(s)")
    _write_json(ARCHIVE_ROWS_CACHE, rows)
    return rows


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to the sheet")
    parser.add_argument("--refresh", action="store_true", help="re-fetch Airtable")
    parser.add_argument(
        "--new-only",
        action="store_true",
        help="only fill blank cells; leave cells that already hold a different date",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    language = selected_language()
    header = language.catalog_pub_date_header
    if header is None:
        print(f"{language.name} has no catalog_code in config/languages.json")
        return 1

    current = [{**row, "_source": "current"} for row in load_current_rows(settings, refresh=args.refresh)]
    archived = load_archive_rows(settings, refresh=args.refresh)

    print("Status by source:")
    by_status = Counter(
        (row["_source"], str(row.get(FIELD_STATUS))) for row in [*current, *archived]
    )
    for (source, status), count in sorted(by_status.items()):
        print(f"  {source:14} {status:28} {count}")

    # One entry per video: the live table wins over archives, newer archives over older.
    from catalog_parser.airtable import normalize_original_video_key

    ordered = [*current, *sorted(archived, key=lambda r: r["_source"], reverse=True)]
    chosen: dict[str, tuple[str | None, date]] = {}
    unparsed = 0
    for row in ordered:
        published = _parse_publish_at(
            row.get(FIELD_SG_YT_DATE),
            publish_timezone=settings.publish_timezone or DEFAULT_PUBLISH_TIMEZONE,
        )
        url = row.get(FIELD_ORIGINAL_VIDEO)
        key = normalize_original_video_key(url)
        if published is None or key is None:
            unparsed += 1
            continue
        chosen.setdefault(key, (url, published.date()))
    entries = list(chosen.values())
    print(
        f"{len(entries)} distinct video(s) to record "
        f"({unparsed} row(s) without a usable link/date skipped)"
    )

    sheets = GoogleSheetsClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    catalog_id = load_catalog_id(PROJECT_ROOT)
    plan = plan_publish_dates(sheets, catalog_id, entries, date_header=header)

    counts = Counter(item.status for item in plan.items)
    print(f"{header}: {dict(counts)}")
    for status in ("changed", "not_found", "ambiguous"):
        for item in plan.with_status(status)[:25]:
            was = f" was {item.previous}" if item.previous else ""
            print(f"  {status}: {item.url} -> {item.publish_date}{was}")

    if not args.apply:
        print("Dry run. Re-run with --apply to write.")
        return 0
    if args.new_only:
        plan = replace(
            plan,
            items=[item for item in plan.items if item.status != "changed"],
        )
    written = apply_plan(sheets, catalog_id, plan)
    print(f"Wrote {written} cell(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
