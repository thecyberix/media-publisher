"""Sync Airtable Original Video Thumbnail attachments from Drive assets.

Strategy:
1. If the Video Folder has a root JPG/PSD/PDF file, fetch the original-platform
   thumbnail from the Original Video link and attach it in Airtable.
2. Otherwise, if the TEXT_ doc has a Canva link, attach the exported Canva image.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.cache_pkgtn_thumbnails import (
    build_filter_formula,
    document_sort_key,
    extract_canva_links,
    fetch_catalog_records,
    index_catalog,
    match_catalog_row,
    parse_folder_id,
    read_word_document,
    status_bucket,
    tn_is_marked,
)
from media_publisher.__main__ import canva_client_from_settings, canva_settings_complete
from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_VIDEO_FOLDER,
    AirtableClient,
    catalog_title,
    has_original_video_thumbnail,
)
from media_publisher.sources.airtable_thumbnail import resolve_thumbnail_attachment
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import SourceThumbnailError

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


_configure_stdio()


def resolve_record_thumbnail(
    *,
    drive: GoogleDriveClient,
    fields: dict,
    folder_cache: dict[str, list],
    doc_cache: dict[str, tuple[list[str], list[str]]],
    canva_client,
) -> tuple[list[dict[str, str]], str, str]:
    folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
    if folder_id is None:
        raise RuntimeError("missing Video Folder link")

    if folder_id not in folder_cache:
        folder_cache[folder_id] = drive.list_children(folder_id)
    children = folder_cache[folder_id]

    canva_url = None
    docs = sorted(
        [
            child
            for child in children
            if child.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
            and child.name.upper().startswith("TEXT_")
        ],
        key=document_sort_key,
    )
    if docs:
        doc = docs[0]
        if doc.id not in doc_cache:
            document = read_word_document(drive, doc)
            doc_cache[doc.id] = extract_canva_links(document) if document else ([], [])
        canva_any, canva_below_tn = doc_cache[doc.id]
        canva_url = (canva_below_tn or canva_any or [None])[0]

    return resolve_thumbnail_attachment(
        children=children,
        original_video_url=str(fields.get(FIELD_ORIGINAL_VIDEO) or ""),
        canva_url=canva_url,
        canva_client=canva_client,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write attachments to Airtable (default is dry-run)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing Original Video Thumbnail attachment",
    )
    parser.add_argument(
        "--pkgtn-only",
        action="store_true",
        help="Only sync videos marked with pkgTn in the SM catalog",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    )
    canva_client = (
        canva_client_from_settings(settings) if canva_settings_complete(settings) else None
    )

    catalog_by_url = catalog_by_title = None
    if args.pkgtn_only:
        catalog = fetch_catalog_records()
        catalog_by_url, catalog_by_title = index_catalog(catalog)

    folder_cache: dict[str, list] = {}
    doc_cache: dict[str, tuple[list[str], list[str]]] = {}
    summary = Counter()
    updates: list[tuple[str, dict, str, str, str]] = []
    failures: list[tuple[str, str]] = []

    records = airtable.list_records(filter_formula=build_filter_formula())
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Sync Airtable thumbnails ({mode}) ===")
    print(f"Records scanned: {len(records)}")
    print()

    for record in records:
        fields = record.fields
        if status_bucket(fields.get(FIELD_STATUS)) is None:
            continue

        if args.pkgtn_only:
            sheet_row = match_catalog_row(fields, catalog_by_url, catalog_by_title)
            if sheet_row is None or not tn_is_marked(sheet_row.get("pkgTn")):
                continue

        title = catalog_title(fields)
        if has_original_video_thumbnail(fields) and not args.force:
            summary["skipped_existing"] += 1
            continue

        try:
            attachment, source_kind, source_detail = resolve_record_thumbnail(
                drive=drive,
                fields=fields,
                folder_cache=folder_cache,
                doc_cache=doc_cache,
                canva_client=canva_client,
            )
        except SourceThumbnailError as exc:
            summary["failed"] += 1
            failures.append((title, str(exc)))
            print(f"FAIL {title}")
            print(f"     {exc}")
            continue
        except RuntimeError:
            summary["no_source"] += 1
            continue
        except Exception as exc:
            summary["failed"] += 1
            failures.append((title, str(exc)))
            print(f"FAIL {title}")
            print(f"     {exc}")
            continue

        summary[source_kind] += 1
        updates.append((record.id, title, source_kind, source_detail, attachment))
        print(f"PLAN {title}")
        print(f"     source: {source_kind} ({source_detail})")
        print(f"     url:    {attachment[0]['url'][:120]}")

    print()
    print("=== Summary ===")
    print(f"Original-platform thumbnails: {summary['original-platform']}")
    print(f"Canva thumbnails:             {summary['canva']}")
    print(f"No qualifying source:         {summary['no_source']}")
    print(f"Skipped (already set):        {summary['skipped_existing']}")
    print(f"Failed:                       {summary['failed']}")
    print(f"Planned updates:              {len(updates)}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to write to Airtable.")
        return 1 if failures else 0

    applied = 0
    for record_id, title, source_kind, _source_detail, attachment in updates:
        try:
            airtable.update_record(
                record_id,
                {FIELD_ORIGINAL_VIDEO_THUMBNAIL: attachment},
            )
            applied += 1
            print(f"OK   {title} ({source_kind})")
        except Exception as exc:
            summary["failed"] += 1
            failures.append((title, str(exc)))
            print(f"FAIL {title}")
            print(f"     {exc}")

    print()
    print(f"Applied: {applied}/{len(updates)}")
    if failures:
        print()
        print("Failures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
