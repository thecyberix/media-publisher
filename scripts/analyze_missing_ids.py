"""Analyze why specific HappyScribe transcription IDs were not found."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TYPE,
    TYPE_QUOTE,
    TYPE_SHORT,
    AirtableClient,
    sync_done_filter_formula,
)
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    find_transcription_for_catalog,
    normalize_name_for_catalog_match,
    resolve_library_location,
)
from scripts.search_sync_smartcat_happyscribe import (
    DEFAULT_PUBLISHED_FOLDER_ID,
    catalog_names_for_record,
    load_search_transcriptions,
)

MISSING_IDS = [
    "2f6cecb118034d4fa5227ff329cd06d2",
    "88b8b1cc1aee46789ae40c6f3abd03c4",
    "0701eb10d7f24874a7c7492028491f72",
]


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    hs = HappyScribeClient(
        settings.happyscribe_api_key or "",
        organization_id="3310225",
    )
    location = resolve_library_location(
        library_url=settings.happyscribe_library_url,
        organization_id=settings.happyscribe_organization_id,
        folder_id=settings.happyscribe_folder_id,
    )
    merged, folder_counts, _ = load_search_transcriptions(
        hs,
        primary=location,
        extra_folder_ids=[DEFAULT_PUBLISHED_FOLDER_ID],
    )
    merged_by_id = {item.id: item for item in merged}

    print("=== HappyScribe transcriptions (by ID) ===")
    for transcription_id in MISSING_IDS:
        transcription = hs.get_transcription(transcription_id)
        in_search_pool = transcription_id in merged_by_id
        print("---")
        print(f"id: {transcription.id}")
        print(f"name: {transcription.name}")
        print(f"state: {transcription.state}")
        print(f"folder_id: {transcription.folder_id}")
        print(f"folder_name: {transcription.folder_name}")
        print(f"in Done+Published search pool: {in_search_pool}")

    print()
    print("Search pool folder counts:", folder_counts)
    print(f"Merged pool size: {len(merged)}")

    at = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    formula = (
        "AND("
        f"{sync_done_filter_formula()}, "
        'FIND("smartcat.com", {Translation resources} & ""), '
        f'{{{FIELD_TYPE}}} != "{TYPE_QUOTE}", '
        f'{{{FIELD_TYPE}}} != "{TYPE_SHORT}"'
        ")"
    )
    records = at.list_records(
        filter_formula=formula,
        fields=[FIELD_TITLE, FIELD_TRANSLATION_RESOURCES, FIELD_TYPE],
    )

    id_set = set(MISSING_IDS)
    print()
    print("=== Airtable name matching against search pool ===")
    for record in records:
        title = (record.fields.get(FIELD_TITLE) or "").strip()
        smartcat = (record.fields.get(FIELD_TRANSLATION_RESOURCES) or "").strip()
        names = catalog_names_for_record(title, smartcat)
        matched_any = any(
            find_transcription_for_catalog(merged, name) is not None for name in names
        )
        if matched_any:
            continue

        print("---")
        print(f"record: {record.id}")
        print(f"title: {title}")
        print(f"names tried: {names}")
        for name in names:
            match = find_transcription_for_catalog(merged, name)
            print(f"  catalog({name!r}) -> {match.name if match else 'NO MATCH'}")
        for transcription_id in MISSING_IDS:
            hs_item = hs.get_transcription(transcription_id)
            for name in names:
                key_airtable = normalize_name_for_catalog_match(name)
                key_hs = normalize_name_for_catalog_match(hs_item.name)
                print(
                    f"  vs {transcription_id}: "
                    f"airtable_key={key_airtable!r} hs_key={key_hs!r} "
                    f"equal={key_airtable == key_hs}"
                )

    print()
    print("=== Which Airtable row maps to which HS ID? (manual title overlap) ===")
    for transcription_id in MISSING_IDS:
        hs_item = hs.get_transcription(transcription_id)
        hs_key = normalize_name_for_catalog_match(hs_item.name)
        print(f"{transcription_id}: {hs_item.name}")
        for record in records:
            title = (record.fields.get(FIELD_TITLE) or "").strip()
            smartcat = (record.fields.get(FIELD_TRANSLATION_RESOURCES) or "").strip()
            for name in catalog_names_for_record(title, smartcat):
                if normalize_name_for_catalog_match(name) == hs_key:
                    print(f"  MATCHES Airtable via {name!r} ({record.id})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
