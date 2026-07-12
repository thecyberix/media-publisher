"""Dump docx block structure for TN debugging."""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_STATUS,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient

WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd"}
STATUS_KEYS = ("To do", "Translation done", "Editing done", "Synchronization done")


def parse_folder_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = FOLDER_ID_RE.search(text)
    return match.group(1) if match else None


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def is_image_file(name: str, mime_type: str) -> bool:
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def dump_document(document: Document) -> None:
    previous = ""
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            text = Paragraph(child, document).text.strip()
            if text:
                previous = text
                if text.upper() in {"TN", "TITLE - YT", "DESCRIPTION"} or "TN" in text.upper():
                    print(f"P: {text[:120]}")
            continue
        if not isinstance(child, CT_Tbl):
            continue
        if previous.upper() not in {"TN", "TITLE - YT", "DESCRIPTION"}:
            previous = ""
            continue
        grid = [[cell.text.strip() for cell in row.cells] for row in Table(child, document).rows]
        print(f"TABLE (after {previous!r}):")
        for row in grid:
            print(f"  {row}")
        previous = ""


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    )
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    formula = f'AND(OR({", ".join(clauses)}), {{Type}} != "{TYPE_QUOTE}")'
    folder_cache: dict[str, list] = {}

    for record in airtable.list_records(filter_formula=formula):
        if status_bucket(record.fields.get(FIELD_STATUS)) is None:
            continue
        folder_id = parse_folder_id(record.fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            continue
        if folder_id not in folder_cache:
            folder_cache[folder_id] = drive.list_children(folder_id)
        images = [c for c in folder_cache[folder_id] if is_image_file(c.name, c.mime_type)]
        if not images:
            continue

        title = catalog_title(record.fields)
        docs = [
            item
            for item in folder_cache[folder_id]
            if item.mime_type == WORD_DOC_MIME and item.name.upper().startswith("TEXT_")
        ]
        print(f"\n=== {title} ===")
        if not docs:
            print("  (no TEXT_ docx)")
            continue
        for doc in docs[:1]:
            print(f"--- {doc.name} ---")
            content = drive._drive.files().get_media(
                fileId=doc.id, supportsAllDrives=True
            ).execute()
            dump_document(Document(io.BytesIO(content)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
