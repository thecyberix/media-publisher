"""Scan all TEXT docs for Canva links immediately below TN caption."""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from docx import Document
from docx.oxml.ns import qn
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
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
CANVA_RE = re.compile(r"https?://(?:www\.)?(?:canva\.com|canva\.link)[^\s\"']*", re.I)
STATUS_KEYS = ("To do", "Translation done", "Editing done", "Synchronization done")


def parse_folder_id(value: object) -> str | None:
    m = FOLDER_ID_RE.search(str(value or "").strip())
    return m.group(1) if m else None


def status_bucket(status: object) -> str | None:
    text = str(status or "")
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def paragraph_urls(paragraph: Paragraph) -> list[str]:
    urls: list[str] = []
    for hyperlink in paragraph._element.xpath(".//w:hyperlink"):
        rel_id = hyperlink.get(qn("r:id"))
        if rel_id and rel_id in paragraph.part.rels:
            urls.append(paragraph.part.rels[rel_id].target_ref)
    urls.extend(CANVA_RE.findall(paragraph.text))
    return urls


def read_word_document(drive, doc):
    if doc.mime_type == WORD_DOC_MIME:
        content = drive._drive.files().get_media(fileId=doc.id, supportsAllDrives=True).execute()
        return Document(io.BytesIO(content))
    if doc.mime_type == GOOGLE_DOC_MIME:
        content = drive._drive.files().export_media(fileId=doc.id, mimeType=WORD_DOC_MIME).execute()
        return Document(io.BytesIO(content))
    return None


def canva_below_tn(document: Document) -> list[str]:
    blocks: list[tuple[str, object]] = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            para = Paragraph(child, document)
            blocks.append(("p", para))
        elif isinstance(child, CT_Tbl):
            grid = [[c.text.strip() for c in r.cells] for r in Table(child, document).rows]
            blocks.append(("t", grid))

    found: list[str] = []
    for i, (kind, payload) in enumerate(blocks):
        if kind != "p" or payload.text.strip() != "TN":
            continue
        for j in range(i + 1, min(len(blocks), i + 4)):
            k, data = blocks[j]
            if k == "t":
                break
            para = data
            text = para.text.strip()
            urls = [u for u in paragraph_urls(para) if CANVA_RE.search(u)]
            if urls:
                found.extend(urls)
            elif text and CANVA_RE.search(text):
                found.extend(CANVA_RE.findall(text))
    return found


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(settings.airtable_token, settings.airtable_base_id, settings.airtable_table_name)
    drive = GoogleDriveClient.from_service_account(PROJECT_ROOT / "credentials" / "google-sheets-service-account.json")
    clauses = [f'FIND("{k}", {{Status}} & "")' for k in STATUS_KEYS]
    formula = f'AND(OR({", ".join(clauses)}), {{Type}} != "{TYPE_QUOTE}")'
    folder_cache: dict[str, list] = {}
    hits = 0
    checked = 0
    for record in airtable.list_records(filter_formula=formula):
        if status_bucket(record.fields.get(FIELD_STATUS)) is None:
            continue
        folder_id = parse_folder_id(record.fields.get(FIELD_VIDEO_FOLDER))
        if not folder_id:
            continue
        if folder_id not in folder_cache:
            folder_cache[folder_id] = drive.list_children(folder_id)
        docs = [c for c in folder_cache[folder_id] if c.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME) and c.name.upper().startswith("TEXT_")]
        if not docs:
            continue
        checked += 1
        doc = sorted(docs, key=lambda d: d.name)[0]
        document = read_word_document(drive, doc)
        if document is None:
            continue
        urls = canva_below_tn(document)
        if urls:
            hits += 1
            title = catalog_title(record.fields)[:70]
            print(f"{title}")
            for url in urls:
                print(f"  {url}")
    print(f"\nChecked {checked} docs, {hits} with Canva below TN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
