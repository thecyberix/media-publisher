"""Find Editing/Sync done videos with thumbnail but no caption or Canva."""
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
    FIELD_TYPE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
    catalog_title,
    has_original_video_thumbnail,
)
from media_publisher.sources.canva import FIELD_CANVA_DESIGN
from media_publisher.sources.google_drive import GoogleDriveClient

STATUS_KEYS = ("Editing done", "Synchronization done")
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
    return f"AND(OR({', '.join(clauses)}), {{Type}} != \"{TYPE_QUOTE}\")"


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


def has_canva_below_tn(document: Document) -> bool:
    blocks: list[tuple[str, object]] = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(("p", Paragraph(child, document)))
        elif isinstance(child, CT_Tbl):
            blocks.append(("t", Table(child, document)))

    for index, (kind, payload) in enumerate(blocks):
        if kind != "p" or payload.text.strip() != TN_LABEL:
            continue
        for next_kind, next_payload in blocks[index + 1 :]:
            if next_kind == "t":
                break
            urls = [url for url in paragraph_urls(next_payload) if CANVA_RE.search(url)]
            if urls:
                return True
        break
    return False


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
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    records = airtable.list_records(filter_formula=build_filter_formula())

    folder_cache: dict[str, list] = {}
    canva_cache: dict[str, bool] = {}
    missing_both: list[dict[str, str | bool]] = []
    missing_either: list[dict[str, str | bool]] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        if not has_original_video_thumbnail(fields):
            continue

        caption = fields.get(FIELD_VIDEO_CAPTION_TRANSLATED)
        has_caption = isinstance(caption, str) and bool(caption.strip())
        canva_field = fields.get(FIELD_CANVA_DESIGN)
        has_canva_field = isinstance(canva_field, str) and bool(canva_field.strip())

        has_canva_doc = False
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id:
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
            if docs:
                doc_id = docs[0].id
                if doc_id not in canva_cache:
                    try:
                        document = read_word_document(drive, docs[0])
                        canva_cache[doc_id] = (
                            has_canva_below_tn(document) if document else False
                        )
                    except Exception:
                        canva_cache[doc_id] = False
                has_canva_doc = canva_cache[doc_id]

        has_canva = has_canva_field or has_canva_doc
        if has_caption and has_canva:
            continue

        entry = {
            "title": catalog_title(fields),
            "status": bucket,
            "record_id": record.id,
            "type": str(fields.get(FIELD_TYPE) or ""),
            "has_caption": has_caption,
            "has_canva": has_canva,
        }
        if not has_caption and not has_canva:
            missing_both.append(entry)
        else:
            missing_either.append(entry)

    print("=== Thumbnail set, missing BOTH translated caption AND Canva link ===")
    print("Statuses: Editing done, Synchronization done")
    print(f"Matches: {len(missing_both)}")
    print()
    for item in sorted(missing_both, key=lambda row: (row["status"], row["title"].casefold())):
        print(f"[{item['status']}] {item['title']}")
        print(f"  record: {item['record_id']} | type: {item['type']}")

    print()
    print("=== Thumbnail set, missing translated caption OR Canva link (but not both) ===")
    print(f"Matches: {len(missing_either)}")
    print()
    for item in sorted(missing_either, key=lambda row: (row["status"], row["title"].casefold())):
        missing = []
        if not item["has_caption"]:
            missing.append("caption")
        if not item["has_canva"]:
            missing.append("Canva")
        print(f"[{item['status']}] {item['title']}")
        print(f"  record: {item['record_id']} | missing: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
