"""Audit TN table text and Canva link below TN caption for catalog videos."""

from __future__ import annotations

import argparse
import io
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DocAudit:
    tn_status: str
    canva_status: str
    doc_name: str | None = None
    canva_url: str | None = None


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


def analyze_document(document: Document) -> DocAudit:
    blocks: list[tuple[str, object]] = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(("p", Paragraph(child, document)))
        elif isinstance(child, CT_Tbl):
            grid = docx_table_to_grid(Table(child, document))
            blocks.append(("t", grid))

    tn_index: int | None = None
    tn_grid: list[list[str]] | None = None
    canva_urls: list[str] = []

    for i, (kind, payload) in enumerate(blocks):
        if kind != "p" or payload.text.strip() != TN_LABEL:
            continue
        tn_index = i
        for j in range(i + 1, len(blocks)):
            next_kind, next_payload = blocks[j]
            if next_kind == "t":
                tn_grid = next_payload
                break
            para = next_payload
            urls = [url for url in paragraph_urls(para) if CANVA_RE.search(url)]
            if urls:
                canva_urls.extend(urls)
        break

    if tn_index is None:
        return DocAudit(tn_status="no_tn_caption", canva_status="n/a")

    if tn_grid is None:
        tn_status = "no_tn_table"
    elif extract_tn_text(tn_grid):
        tn_status = "filled"
    else:
        tn_status = "empty"

    if canva_urls:
        unique = list(dict.fromkeys(canva_urls))
        return DocAudit(
            tn_status=tn_status,
            canva_status="present",
            canva_url=unique[0],
        )
    return DocAudit(tn_status=tn_status, canva_status="missing")


def document_sort_key(item) -> tuple[int, str]:
    name = item.name
    text_prefix_rank = 0 if name.upper().startswith("TEXT_") else 1
    return (text_prefix_rank, name.casefold())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-gaps",
        action="store_true",
        help="List videos missing TN text and/or Canva link",
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
    records = airtable.list_records(filter_formula=build_filter_formula())

    folder_cache: dict[str, list] = {}
    doc_cache: dict[str, DocAudit] = {}
    rows: list[dict] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue

        title = catalog_title(fields)
        video_type = str(fields.get(FIELD_TYPE) or "(none)")
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))

        if folder_id is None:
            rows.append(
                {
                    "title": title,
                    "status": bucket,
                    "type": video_type,
                    "audit": DocAudit("no_folder", "n/a"),
                    "doc_name": None,
                }
            )
            continue

        if folder_id not in folder_cache:
            folder_cache[folder_id] = drive.list_children(folder_id)
        children = folder_cache[folder_id]
        docs = sorted(
            [
                item
                for item in children
                if item.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
                and item.name.upper().startswith("TEXT_")
            ],
            key=document_sort_key,
        )

        if not docs:
            rows.append(
                {
                    "title": title,
                    "status": bucket,
                    "type": video_type,
                    "audit": DocAudit("no_text_doc", "n/a"),
                    "doc_name": None,
                }
            )
            continue

        doc = docs[0]
        if doc.id not in doc_cache:
            try:
                document = read_word_document(drive, doc)
                doc_cache[doc.id] = (
                    analyze_document(document) if document else DocAudit("doc_error", "n/a")
                )
            except Exception:
                doc_cache[doc.id] = DocAudit("doc_error", "n/a")
        audit = doc_cache[doc.id]
        rows.append(
            {
                "title": title,
                "status": bucket,
                "type": video_type,
                "audit": DocAudit(
                    tn_status=audit.tn_status,
                    canva_status=audit.canva_status,
                    doc_name=doc.name,
                    canva_url=audit.canva_url,
                ),
                "doc_name": doc.name,
            }
        )

    print("=== TN + Canva audit (TEXT_ doc in Video Folder) ===")
    print(f"Total videos: {len(rows)}")
    print()

    summary: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    combo: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for row in rows:
        bucket = row["status"]
        audit: DocAudit = row["audit"]
        summary[bucket]["total"] += 1
        summary[bucket][f"tn_{audit.tn_status}"] += 1
        if audit.tn_status in {"filled", "empty", "no_tn_table", "no_tn_caption"}:
            summary[bucket][f"canva_{audit.canva_status}"] += 1
            combo[bucket][f"{audit.tn_status}+{audit.canva_status}"] += 1

    for key in STATUS_KEYS:
        stats = summary[key]
        if not stats.get("total"):
            continue
        total = stats["total"]
        filled = stats.get("tn_filled", 0)
        empty = stats.get("tn_empty", 0)
        no_table = stats.get("tn_no_tn_table", 0)
        no_caption = stats.get("tn_no_tn_caption", 0)
        no_doc = stats.get("tn_no_text_doc", 0)
        no_folder = stats.get("tn_no_folder", 0)
        doc_err = stats.get("tn_doc_error", 0)
        canva_present = stats.get("canva_present", 0)
        canva_missing = stats.get("canva_missing", 0)
        both_ready = combo[key].get("filled+present", 0)

        print(f"{key} ({total} videos):")
        print(f"  TN filled:           {filled} ({100 * filled / total:.0f}%)")
        print(f"  TN empty table:      {empty}")
        print(f"  TN table missing:    {no_table}")
        print(f"  TN caption missing:  {no_caption}")
        print(f"  No TEXT_ doc:        {no_doc}")
        print(f"  No Video Folder:     {no_folder}")
        if doc_err:
            print(f"  Doc read errors:     {doc_err}")
        tn_docs = filled + empty + no_table + no_caption
        if tn_docs:
            print(
                f"  Canva below TN:      {canva_present} present, "
                f"{canva_missing} missing (of {tn_docs} with TN caption)"
            )
        print(f"  Ready (TN+Canva):    {both_ready}")
        print()

    overall = defaultdict(int)
    for row in rows:
        audit: DocAudit = row["audit"]
        overall["total"] += 1
        overall[f"tn_{audit.tn_status}"] += 1
        if audit.canva_status in {"present", "missing"}:
            overall[f"canva_{audit.canva_status}"] += 1
        if audit.tn_status == "filled" and audit.canva_status == "present":
            overall["ready"] += 1

    total = overall["total"]
    print("=== Overall ===")
    print(f"Total:               {total}")
    print(f"TN filled:           {overall.get('tn_filled', 0)}")
    print(f"Canva below TN:      {overall.get('canva_present', 0)}")
    print(f"Both TN + Canva:     {overall.get('ready', 0)}")
    print()

    if args.show_gaps:
        print("=== Gaps: missing filled TN and/or Canva below TN ===")
        for key in STATUS_KEYS:
            gaps = [
                row
                for row in rows
                if row["status"] == key
                and (
                    row["audit"].tn_status != "filled"
                    or row["audit"].canva_status != "present"
                )
            ]
            if not gaps:
                continue
            print(f"\n--- {key}: {len(gaps)} ---")
            for row in sorted(gaps, key=lambda item: item["title"]):
                audit: DocAudit = row["audit"]
                parts = [f"TN={audit.tn_status}", f"Canva={audit.canva_status}"]
                if audit.doc_name:
                    parts.append(f"doc={audit.doc_name}")
                print(f"  - {row['title']} ({', '.join(parts)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
