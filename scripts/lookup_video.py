"""Look up one catalog title in Airtable and HappyScribe."""
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
    AirtableClient,
)
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    catalog_names_for_record,
    find_transcription_for_catalog_names,
    is_subtitled_export_name,
    normalize_name_for_catalog_match,
    resolve_library_location,
    srt_name_from_smartcat_url,
)


def main(argv: list[str]) -> int:
    target = (
        argv[1]
        if len(argv) > 1
        else "Ask Your Deepest Questions To Sadhguru Soak In Ecstasy Of Enlightenment"
    )
    settings = load_settings(PROJECT_ROOT)

    at = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    needle = target.replace('"', '\\"')
    records = at.list_records(
        filter_formula=f'FIND("{needle[:40]}", {{{FIELD_TITLE}}} & "")',
        fields=[FIELD_TITLE, FIELD_STATUS, FIELD_TYPE, FIELD_TRANSLATION_RESOURCES],
    )

    print(f"=== Airtable lookup: {target!r} ===")
    if not records:
        print("No Airtable record found.")
    for rec in records:
        fields = rec.fields
        title = (fields.get(FIELD_TITLE) or "").strip()
        if needle[:20].casefold() not in title.casefold():
            continue
        smartcat = (fields.get(FIELD_TRANSLATION_RESOURCES) or "").strip()
        print(f"id: {rec.id}")
        print(f"type: {fields.get(FIELD_TYPE)}")
        print(f"status: {fields.get(FIELD_STATUS)}")
        print(f"title: {title}")
        print(f"smartcat: {smartcat}")
        print(f"srt from url: {srt_name_from_smartcat_url(smartcat)}")

    hs = HappyScribeClient(
        settings.happyscribe_api_key or "",
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
    print()
    print("=== HappyScribe search (Done + Published) ===")
    print(f"merged transcriptions: {len(transcriptions)}")

    names = [target]
    if records:
        for rec in records:
            title = (rec.fields.get(FIELD_TITLE) or "").strip()
            smartcat = (rec.fields.get(FIELD_TRANSLATION_RESOURCES) or "").strip()
            if title:
                names = catalog_names_for_record(title, smartcat)
                break

    print(f"names to try: {names}")
    match, matched_by = find_transcription_for_catalog_names(transcriptions, names)
    if match is not None:
        print("FOUND")
        print(f"  matched by: {matched_by!r}")
        print(f"  id: {match.id}")
        print(f"  name: {match.name}")
        print(f"  state: {match.state}")
        print(f"  folder: {match.folder_name or match.folder_id}")
        return 0

    print("NOT FOUND")
    key = normalize_name_for_catalog_match(target)
    close: list[str] = []
    for transcription in transcriptions:
        if is_subtitled_export_name(transcription.name):
            continue
        item_key = normalize_name_for_catalog_match(transcription.name)
        if "deepest" in item_key and "questions" in item_key:
            close.append(transcription.name)
        elif key[:20] in item_key or item_key[:20] in key:
            close.append(transcription.name)
    if close:
        print("close matches:")
        for name in close[:10]:
            print(f"  {name}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
