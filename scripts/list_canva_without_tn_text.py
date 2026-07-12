"""List TEXT docs with Canva below TN but missing filled TN caption text."""
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

STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
TN_LABEL = "TN"
CANVA_RE = re.compile(r"https?://(?:www\.)?(?:canva\.com|canva\.link)[^\s\"']*", re.I)


def parse_folder_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = FOLDER_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def docx_table_to_grid(table: Table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


def paragraph_urls(paragraph: Paragraph) -> list[str]:
    urls: list[str] = []
    for hyperlink in paragraph._element.xpath(".//w:hyperlink"):
        rel_id = hyperlink.get(qn("r:id"))
        if rel_id and rel_id in paragraph.part.rels:
            urls.append(paragraph.part.rels[rel_id].target_ref)
    urls.extend(CANVA_RE.findall(paragraph.text))
    return urls


def read_word_document(drive: GoogleDriveClient, doc) -> Document | None:
    if doc.mime_type == WORD_DOC_MIME:
        content = drive._drive.files().get_media(
            fileId=doc.id, supportsAllDrives=True
        ).execute()
        return Document(io.BytesIO(content))
    if doc.mime_type == GOOGLE_DOC_MIME:
        content = (
            drive._drive.files()
            .export_media(fileId=doc.id, mimeType=WORD_DOC_MIME)
            .execute()
        )
        return Document(io.BytesIO(content))
    return None


def extract_tn_text(grid: list[list[str]]) -> str | None:
    rows = [[cell.strip() for cell in row] for row in grid if row]
    if not rows:
        return None
    header_cells = [_normalize_label(cell) for cell in rows[0]]
    data_rows = rows[1:] if header_cells == [
        _normalize_label("english"),
        _normalize_label("language"),
    ] else rows
    english_parts = [row[0] for row in data_rows if row and row[0]]
    text = "\n".join(english_parts).strip()
    return text or None


def analyze_document(document: Document) -> tuple[str, str | None]:
    blocks: list[tuple[str, object]] = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(("p", Paragraph(child, document)))
        elif isinstance(child, CT_Tbl):
            blocks.append(("t", docx_table_to_grid(Table(child, document))))

    tn_grid: list[list[str]] | None = None
    canva_urls: list[str] = []

    for i, (kind, payload) in enumerate(blocks):
        if kind != "p" or payload.text.strip() != TN_LABEL:
            continue
        for j in range(i + 1, len(blocks)):
            next_kind, next_payload = blocks[j]
            if next_kind == "t":
                tn_grid = next_payload
                break
            urls = [url for url in paragraph_urls(next_payload) if CANVA_RE.search(url)]
            if urls:
                canva_urls.extend(urls)
        break
    else:
        return "no_tn_caption", None

    if tn_grid is None:
        tn_status = "no_tn_table"
    elif extract_tn_text(tn_grid):
        tn_status = "filled"
    else:
        tn_status = "empty"

    canva_url = list(dict.fromkeys(canva_urls))[0] if canva_urls else None
    return tn_status, canva_url


def drive_file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def document_sort_key(item) -> tuple[int, str]:
    return (0 if item.name.upper().startswith("TEXT_") else 1, item.name.casefold())


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
    records = airtable.list_records(filter_formula=build_filter_formula())

    folder_cache: dict[str, list] = {}
    doc_cache: dict[str, tuple[str, str | None]] = {}
    matches: list[dict] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue

        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            continue

        if folder_id not in folder_cache:
            folder_cache[folder_id] = drive.list_children(folder_id)
        docs = sorted(
            [
                item
                for item in folder_cache[folder_id]
                if item.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
                and item.name.upper().startswith("TEXT_")
            ],
            key=document_sort_key,
        )
        if not docs:
            continue

        doc = docs[0]
        if doc.id not in doc_cache:
            document = read_word_document(drive, doc)
            doc_cache[doc.id] = analyze_document(document) if document else ("doc_error", None)
        tn_status, canva_url = doc_cache[doc.id]

        if canva_url and tn_status != "filled":
            matches.append(
                {
                    "title": catalog_title(fields),
                    "status": bucket,
                    "tn_status": tn_status,
                    "doc_name": doc.name,
                    "doc_url": drive_file_url(doc.id),
                    "canva_url": canva_url,
                }
            )

    print(f"Found {len(matches)} doc(s) with Canva link but missing TN caption text\n")
    for item in sorted(matches, key=lambda row: (row["status"], row["title"])):
        print(item["title"])
        print(f"  status:   {item['status']}")
        print(f"  TN issue: {item['tn_status']}")
        print(f"  doc:      {item['doc_url']}")
        print(f"  canva:    {item['canva_url']}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
