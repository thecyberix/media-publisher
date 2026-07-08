"""Cross-reference Airtable sync-done + SmartCat rows with HappyScribe library."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TYPE,
    TYPE_QUOTE,
    TYPE_SHORT,
    AirtableClient,
    sync_done_filter_formula,
)
from media_publisher.sources.happyscribe import (
    TRANSCRIPTION_STATE_READY,
    HappyScribeClient,
    catalog_names_for_record,
    find_transcription_for_catalog_names,
    resolve_library_location,
    srt_name_from_smartcat_url,
)


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    client = AirtableClient(
        token=settings.airtable_token,
        base_id=settings.airtable_base_id,
        table_name=settings.airtable_table_name,
        api_base=settings.airtable_api_base,
        view=settings.airtable_view,
    )

    smartcat_formula = (
        "AND("
        f"{sync_done_filter_formula()}, "
        'FIND("smartcat.com", {Translation resources} & ""), '
        f'{{{FIELD_TYPE}}} != "{TYPE_QUOTE}", '
        f'{{{FIELD_TYPE}}} != "{TYPE_SHORT}"'
        ")"
    )
    records = client.list_records(
        filter_formula=smartcat_formula,
        fields=[FIELD_TITLE, FIELD_STATUS, FIELD_TRANSLATION_RESOURCES, FIELD_TYPE],
    )

    hs = HappyScribeClient(
        api_key=settings.happyscribe_api_key or "",
        api_base=settings.happyscribe_api_base,
        organization_id=settings.happyscribe_organization_id,
    )
    location = resolve_library_location(
        library_url=settings.happyscribe_library_url,
        organization_id=settings.happyscribe_organization_id,
        folder_id=settings.happyscribe_folder_id,
    )
    extra_folders = (
        [settings.happyscribe_published_folder_id]
        if settings.happyscribe_published_folder_id
        else []
    )
    transcriptions = hs.list_search_transcriptions(
        location,
        extra_folder_ids=extra_folders,
    )
    folder_counts = {
        location.folder_id: len(hs.list_library_transcriptions(location)),
    }
    if settings.happyscribe_published_folder_id:
        from media_publisher.sources.happyscribe import HappyScribeLibraryLocation

        published_location = HappyScribeLibraryLocation(
            organization_id=location.organization_id,
            folder_id=settings.happyscribe_published_folder_id,
        )
        folder_counts[settings.happyscribe_published_folder_id] = len(
            hs.list_library_transcriptions(published_location)
        )

    found: list[tuple] = []
    missing: list[tuple] = []
    not_ready: list[tuple] = []
    for rec in records:
        title = (rec.fields.get(FIELD_TITLE) or "").strip()
        smartcat = (rec.fields.get(FIELD_TRANSLATION_RESOURCES) or "").strip()
        status = (rec.fields.get(FIELD_STATUS) or "").strip()
        record_type = (rec.fields.get(FIELD_TYPE) or "").strip()
        srt_name = srt_name_from_smartcat_url(smartcat)
        catalog_names = catalog_names_for_record(title, smartcat)
        match, matched_by = find_transcription_for_catalog_names(
            transcriptions,
            catalog_names,
        )
        if match is None:
            missing.append((rec.id, title, status, record_type, smartcat, srt_name))
        elif match.state != TRANSCRIPTION_STATE_READY:
            not_ready.append(
                (
                    rec.id,
                    title,
                    status,
                    record_type,
                    srt_name,
                    matched_by,
                    match.id,
                    match.name,
                    match.folder_name or match.folder_id or "?",
                    match.state,
                )
            )
        else:
            found.append(
                (
                    rec.id,
                    title,
                    status,
                    record_type,
                    srt_name,
                    matched_by,
                    match.id,
                    match.name,
                    match.folder_name or match.folder_id or "?",
                    match.state,
                )
            )

    print("=== Airtable: Synchronization done + SmartCat (Video/Reel, not Short/Quote) ===")
    print(f"Total matching Airtable records: {len(records)}")
    print("HappyScribe folders searched:")
    for folder_id, count in sorted(folder_counts.items()):
        label = "Done" if folder_id == location.folder_id else "Published"
        print(f"  {folder_id} ({label}): {count} transcription(s)")
    print(f"HappyScribe unique transcriptions (merged): {len(transcriptions)}")
    print()
    print(f"Found in HappyScribe (ready): {len(found)}")
    print(f"Found but not ready: {len(not_ready)}")
    print(f"Not found in HappyScribe: {len(missing)}")
    print()

    if missing:
        print("--- NOT FOUND IN HAPPYSCRIBE ---")
        for rid, title, status, record_type, _smartcat, srt_name in missing:
            srt_hint = f"\tSRT:{srt_name}" if srt_name else "\tSRT:(not in URL)"
            print(f"{rid}\t{record_type}\t{title}\t{status}{srt_hint}")

    print()
    print("--- MATCHES IN HAPPYSCRIBE ---")
    for rid, title, status, record_type, srt_name, matched_by, tid, tname, folder, tstate in found:
        print(
            f"{rid}\t{record_type}\t{title}\tSRT:{srt_name or '-'}\t"
            f"matched:{matched_by}\t->\t{tid}\t{tname}\t"
            f"hs_folder:{folder}\t{tstate}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
