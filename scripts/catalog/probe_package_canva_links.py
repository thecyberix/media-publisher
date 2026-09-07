"""Probe Video Folder docs for Canva links (field-code aware).

Usage:
  python scripts/catalog/probe_package_canva_links.py --title "Ancient Hindu..."
  python scripts/catalog/probe_package_canva_links.py --today-aspect-mismatch
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from docx import Document

from catalog_parser.__main__ import load_env_file
from catalog_parser.airtable import (
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_TITLE,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.auth import get_docs_service, get_drive_service_noninteractive
from catalog_parser.canva_selection import (
    extract_canva_links_from_docx,
    extract_canva_links_from_google_document,
)
from catalog_parser.drive_docs import (
    GOOGLE_DOC_MIME_TYPE,
    WORD_DOC_MIME_TYPE,
    extract_drive_folder_id,
    list_text_documents_in_folder,
)
from media_publisher.sources.airtable import (
    apply_airtable_url_env,
    has_original_video_thumbnail,
    parse_airtable_url,
)

# Titles ingested 2026-09-07 that logged:
# "no thumbnail source (skipped review: aspect mismatch)"
TODAY_ASPECT_MISMATCH_TITLES = (
    "Ancient Hindu Calendar Secrets | Sadhguru 360",
    "How Yogis Access Cosmic Secrets | Sadhguru 360",
    "The Truth About Intermittent Fasting | Sadhguru",
)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _airtable_client() -> AirtableClient:
    apply_airtable_url_env()
    url = _require_env("AIRTABLE_URL")
    base_id, table_id = parse_airtable_url(url)
    return AirtableClient(
        token=_require_env("AIRTABLE_TOKEN"),
        base_id=base_id,
        table_name=table_id,
    )


def _find_records_by_titles(
    airtable: AirtableClient,
    titles: list[str],
) -> list[dict]:
    wanted = {title.casefold(): title for title in titles}
    found: dict[str, dict] = {}
    for record in airtable.list_records():
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        title = fields.get(FIELD_TITLE)
        if not isinstance(title, str):
            continue
        key = title.casefold()
        if key in wanted and key not in found:
            found[key] = record
    missing = [wanted[k] for k in wanted if k not in found]
    for title in missing:
        print(f"MISSING Airtable row: {title}")
    return [found[title.casefold()] for title in titles if title.casefold() in found]


def _scan_folder_canva(
    drive_service,
    docs_service,
    folder_id: str,
) -> list[tuple[str, str, list[str]]]:
    """Return (doc_name, mime, canva_urls) for each text document."""
    results: list[tuple[str, str, list[str]]] = []
    for document in list_text_documents_in_folder(drive_service, folder_id):
        document_id = document.get("id")
        mime_type = document.get("mimeType")
        name = document.get("name") or document_id or "?"
        if not isinstance(document_id, str) or not isinstance(mime_type, str):
            continue
        try:
            if mime_type == WORD_DOC_MIME_TYPE:
                content = (
                    drive_service.files()
                    .get_media(fileId=document_id, supportsAllDrives=True)
                    .execute()
                )
                docx_document = Document(io.BytesIO(content))
                urls, _below = extract_canva_links_from_docx(docx_document)
            elif mime_type == GOOGLE_DOC_MIME_TYPE and docs_service is not None:
                google_document = (
                    docs_service.documents().get(documentId=document_id).execute()
                )
                if not isinstance(google_document, dict):
                    continue
                urls, _below = extract_canva_links_from_google_document(google_document)
            else:
                continue
        except Exception as exc:
            print(f"  WARN scan failed for {name!r}: {exc}")
            continue
        results.append((name, mime_type, urls))
    return results


def _probe_record(drive_service, docs_service, record: dict) -> None:
    fields = record.get("fields") or {}
    title = fields.get(FIELD_TITLE) or record.get("id")
    folder_link = fields.get(FIELD_VIDEO_FOLDER)
    has_thumb = has_original_video_thumbnail(fields)
    print(f"\n=== {title} ===")
    print(f"record: {record.get('id')}")
    print(f"Original Video Thumbnail set: {has_thumb}")
    if has_thumb:
        print(f"  attachments: {fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)}")
    if not isinstance(folder_link, str) or not folder_link.strip():
        print("Video Folder: MISSING")
        return
    folder_id = extract_drive_folder_id(folder_link)
    print(f"Video Folder: {folder_link}")
    if folder_id is None:
        print("  could not parse folder id")
        return
    scans = _scan_folder_canva(drive_service, docs_service, folder_id)
    if not scans:
        print("  no Word/Google docs in folder")
        return
    any_canva = False
    for name, mime, urls in scans:
        kind = "docx" if mime == WORD_DOC_MIME_TYPE else "gdoc"
        if urls:
            any_canva = True
            print(f"  [{kind}] {name}: FOUND {len(urls)} Canva link(s)")
            for url in urls:
                print(f"    - {url}")
        else:
            print(f"  [{kind}] {name}: no Canva link detected")
    if any_canva and not has_thumb:
        print("  => Would now stage Canva thumbnail (was previously missed)")
    elif not any_canva and not has_thumb:
        print("  => Still no Canva link; aspect-mismatch skip was unrelated to field codes")


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", action="append", default=[], help="Exact Airtable Title")
    parser.add_argument(
        "--today-aspect-mismatch",
        action="store_true",
        help="Probe titles from 2026-09-07 aspect-mismatch ingest misses",
    )
    args = parser.parse_args()
    titles = list(args.title)
    if args.today_aspect_mismatch:
        titles.extend(TODAY_ASPECT_MISMATCH_TITLES)
    if not titles:
        parser.error("Provide --title and/or --today-aspect-mismatch")

    airtable = _airtable_client()
    drive = get_drive_service_noninteractive()
    docs = get_docs_service(Path("credentials.json"), Path("token.json"))
    records = _find_records_by_titles(airtable, titles)
    for record in records:
        _probe_record(drive, docs, record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
