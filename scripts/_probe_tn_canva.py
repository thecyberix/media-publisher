"""Probe TN caption + Canva link structure in sample docs."""
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
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
CANVA_RE = re.compile(r"canva\.(?:com|link)", re.I)


def parse_folder_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = FOLDER_ID_RE.search(text)
    return match.group(1) if match else None


def paragraph_hyperlinks(paragraph: Paragraph) -> list[str]:
    urls: list[str] = []
    for rel in paragraph.part.rels.values():
        if rel.reltype.endswith("/hyperlink"):
            urls.append(rel.target_ref)
    # also scan runs for visible canva text
    return urls


def read_word_document(drive, doc):
    if doc.mime_type == WORD_DOC_MIME:
        content = drive._drive.files().get_media(fileId=doc.id, supportsAllDrives=True).execute()
        return Document(io.BytesIO(content))
    if doc.mime_type == GOOGLE_DOC_MIME:
        content = drive._drive.files().export_media(fileId=doc.id, mimeType=WORD_DOC_MIME).execute()
        return Document(io.BytesIO(content))
    return None


def dump_tn_context(document: Document) -> None:
    blocks = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            para = Paragraph(child, document)
            text = para.text.strip()
            links = paragraph_hyperlinks(para)
            blocks.append(("p", text, links))
        elif isinstance(child, CT_Tbl):
            grid = [[c.text.strip() for c in r.cells] for r in Table(child, document).rows]
            blocks.append(("t", grid, []))

    for i, (kind, payload, links) in enumerate(blocks):
        if kind != "p" or payload != "TN":
            continue
        print("  --- blocks around TN ---")
        for j in range(max(0, i - 1), min(len(blocks), i + 4)):
            k, data, lnks = blocks[j]
            marker = ">>" if j == i else "  "
            if k == "p":
                print(f"{marker} P: {data!r} links={lnks}")
                if CANVA_RE.search(data):
                    print("     (canva in visible text)")
            else:
                print(f"{marker} TABLE: {data[:2]}...")


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token, settings.airtable_base_id, settings.airtable_table_name
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    )
    samples = [
        "Krishna: Untold Stories",
        "Farm vs Supermarket",
        "Truth Behind Allegations",
        "Dont Let Your Past",
    ]
    for record in airtable.list_records(
        filter_formula=f'AND({{Type}} != "{TYPE_QUOTE}", FIND("To do", {{Status}} & ""))'
    ):
        title = catalog_title(record.fields)
        if not any(s in title for s in samples):
            continue
        folder_id = parse_folder_id(record.fields.get(FIELD_VIDEO_FOLDER))
        if not folder_id:
            continue
        children = drive.list_children(folder_id)
        docs = sorted(
            [c for c in children if c.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)],
            key=lambda d: (0 if d.name.upper().startswith("TEXT_") else 1, d.name),
        )
        print(f"\n=== {title} ===")
        for doc in docs[:1]:
            print(f"doc: {doc.name}")
            document = read_word_document(drive, doc)
            if document:
                dump_tn_context(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
