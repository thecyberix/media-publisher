from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.__main__ import (
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    enrich_single_record_with_smartcat_web,
    load_env_file,
)
from catalog_parser.airtable import AirtableClient, load_existing_titles_for_ingest
from catalog_parser.auth import get_docs_service, get_drive_service, get_sheets_service
from catalog_parser.drive_docs import enrich_records_with_yt_titles
from catalog_parser.eligibility import explain_catalog_eligibility
from catalog_parser.parser import extract_sheet_id, parse_catalog, type_duration_bounds
from catalog_parser.smartcat_web import DEFAULT_STORAGE_STATE, SmartcatWebClient, SmartcatWebSession
from catalog_parser.workflow.table_cache import TableCache


def diagnose_record(
    record: dict,
    *,
    existing_titles: set[str],
    drive_service,
    docs_service,
    smartcat_session: SmartcatWebSession | None,
    smartcat_language: str,
) -> list[str]:
    enriched = dict(record)
    if smartcat_session is not None:
        enriched = enrich_single_record_with_smartcat_web(
            enriched,
            smartcat_session,
            smartcat_language=smartcat_language,
        )
    enriched = enrich_records_with_yt_titles([enriched], drive_service, docs_service)[0]

    return explain_catalog_eligibility(
        enriched,
        existing_titles,
        drive_service=drive_service,
        require_smartcat=True,
        require_mixable_media=True,
    ) or ["Eligible"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose ingest eligibility for catalog candidates.")
    parser.add_argument(
        "--positions",
        default="2,3,4",
        help="1-based candidate positions after pub-date sort (default: 2,3,4).",
    )
    parser.add_argument(
        "--type",
        dest="video_type",
        default="Reel",
        choices=["Reel", "Short", "Video"],
    )
    args = parser.parse_args()
    positions = [int(item.strip()) for item in args.positions.split(",") if item.strip()]

    load_env_file(PROJECT_ROOT / ".env")
    sheet_id = os.getenv("SHEET_ID", "").strip()
    if not sheet_id:
        print("Missing SHEET_ID", file=sys.stderr)
        return 1

    min_duration, max_duration = type_duration_bounds(args.video_type)
    sheets = get_sheets_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    candidates = parse_catalog(
        sheets,
        extract_sheet_id(sheet_id),
        sheet_name=os.getenv("SHEET_NAME") or None,
        sheet_range=os.getenv("SHEET_RANGE") or None,
        limit=0,
        min_duration=min_duration,
        max_duration=max_duration,
        video_type=args.video_type,
    )
    print(f"{args.video_type} candidates after pub-date sort: {len(candidates)}")

    airtable = AirtableClient(
        token=os.environ["AIRTABLE_TOKEN"],
        base_id=os.environ["AIRTABLE_BASE_ID"],
        table_name=os.environ["AIRTABLE_TABLE_NAME"],
    )
    table_cache = TableCache.load(
        airtable,
        project_root=PROJECT_ROOT,
        backup=False,
        record_status_history=False,
    )
    existing_titles = load_existing_titles_for_ingest(
        airtable,
        table_cache=table_cache,
        project_root=PROJECT_ROOT,
    )
    print(f"Airtable table: {os.environ['AIRTABLE_TABLE_NAME']!r}")
    print(f"Airtable titles loaded: {len(existing_titles)}")

    drive_service = get_drive_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    docs_service = get_docs_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=False)
    smartcat_language = os.getenv("SMARTCAT_TARGET_LANGUAGE", "bg")
    storage_state_path = Path(os.getenv("SMARTCAT_STORAGE_STATE", DEFAULT_STORAGE_STATE))
    web_client = SmartcatWebClient(
        ui_base=os.getenv("SMARTCAT_UI_BASE", "https://ea.smartcat.com").strip() or "https://ea.smartcat.com",
        storage_state_path=storage_state_path,
        headless=True,
        language=smartcat_language,
    )

    with SmartcatWebSession(web_client) as session:
        for position in positions:
            if position < 1 or position > len(candidates):
                print(f"\n#{position}: out of range")
                continue
            candidate = candidates[position - 1]
            reasons = diagnose_record(
                candidate,
                existing_titles=existing_titles,
                drive_service=drive_service,
                docs_service=docs_service,
                smartcat_session=session,
                smartcat_language=smartcat_language,
            )
            print()
            print(f"#{position}: {candidate.get('ctPubDate')} | {candidate.get('ctTitle')}")
            for reason in reasons:
                print(f"  - {reason}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
