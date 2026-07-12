"""Check TN thumbnail text tables in Drive docx files for videos with root images."""
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
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd",
}
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
TN_LABEL = "TN"
TITLE_YT_LABEL = "TITLE - YT"
DESCRIPTION_LABEL = "Description"


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


def is_image_file(name: str, mime_type: str) -> bool:
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def docx_table_to_grid(table: Table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


def iter_docx_blocks(document: Document):
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield "paragraph", Paragraph(child, document).text
        elif isinstance(child, CT_Tbl):
            yield "table", docx_table_to_grid(Table(child, document))


def table_matches_label(
    grid: list[list[str]],
    preceding_text: str | None,
    label: str,
) -> bool:
    if preceding_text and _normalize_label(preceding_text) == _normalize_label(label):
        return True
    if not grid:
        return False
    first_row = " ".join(grid[0]).strip()
    return _normalize_label(label) in _normalize_label(first_row)


def extract_labeled_table(
    document: Document,
    label: str,
) -> list[list[str]] | None:
    previous_text = ""
    for block_type, block in iter_docx_blocks(document):
        if block_type == "paragraph":
            text = str(block).strip()
            if text:
                previous_text = text
            continue
        grid = block
        if table_matches_label(grid, previous_text, label):
            return grid
        previous_text = ""
    return None


def read_word_document(drive: GoogleDriveClient, doc) -> Document | None:
    if doc.mime_type == WORD_DOC_MIME:
        content = drive._drive.files().get_media(
            fileId=doc.id, supportsAllDrives=True
        ).execute()
        return Document(io.BytesIO(content))
    if doc.mime_type == GOOGLE_DOC_MIME:
        content = (
            drive._drive.files()
            .export_media(
                fileId=doc.id,
                mimeType=WORD_DOC_MIME,
            )
            .execute()
        )
        return Document(io.BytesIO(content))
    return None


def extract_tn_text(grid: list[list[str]]) -> dict[str, str | None]:
    rows = [[cell.strip() for cell in row] for row in grid if row]
    if not rows:
        return {"english": None, "bulgarian": None}

    header_cells = [_normalize_label(cell) for cell in rows[0]]
    if header_cells == [_normalize_label("english"), _normalize_label("language")]:
        data_rows = rows[1:]
    else:
        data_rows = rows

    english_parts: list[str] = []
    bulgarian_parts: list[str] = []
    for row in data_rows:
        if not row:
            continue
        if len(row) >= 1 and row[0]:
            english_parts.append(row[0])
        if len(row) >= 2 and row[1]:
            bulgarian_parts.append(row[1])

    english = "\n".join(part for part in english_parts if part).strip() or None
    bulgarian = "\n".join(part for part in bulgarian_parts if part).strip() or None
    return {"english": english, "bulgarian": bulgarian}


def document_sort_key(item) -> tuple[int, str]:
    name = item.name
    text_prefix_rank = 0 if name.upper().startswith("TEXT_") else 1
    return (text_prefix_rank, name.casefold())


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
    targets: list[dict] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            continue
        if folder_id not in folder_cache:
            children = drive.list_children(folder_id)
            folder_cache[folder_id] = children
        children = folder_cache[folder_id]
        images = [c for c in children if is_image_file(c.name, c.mime_type)]
        if not images:
            continue
        docs = sorted(
            [c for c in children if c.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)],
            key=document_sort_key,
        )
        targets.append(
            {
                "title": catalog_title(fields),
                "status": bucket,
                "type": str(fields.get(FIELD_TYPE) or "(none)"),
                "folder_id": folder_id,
                "docs": docs,
            }
        )

    print(f"=== TN text check ({len(targets)} videos with root image) ===\n")

    tn_ok = 0
    tn_missing = 0
    tn_empty = 0
    no_doc = 0

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        print(f"{item['title']}")
        print(f"  status: {item['status']} | type: {item['type']}")

        if not item["docs"]:
            no_doc += 1
            print("  doc:    none")
            print("  TN:     MISSING (no docx/google doc in folder)")
            print()
            continue

        doc_names = ", ".join(d.name for d in item["docs"][:3])
        if len(item["docs"]) > 3:
            doc_names += f" (+{len(item['docs']) - 3} more)"
        print(f"  doc:    {doc_names}")

        tn_grid: list[list[str]] | None = None
        tn_source: str | None = None
        title_grid: list[list[str]] | None = None
        desc_grid: list[list[str]] | None = None

        for doc in item["docs"]:
            document = read_word_document(drive, doc)
            if document is None:
                continue
            if tn_grid is None:
                candidate = extract_labeled_table(document, TN_LABEL)
                if candidate is not None:
                    tn_grid = candidate
                    tn_source = f"{doc.name} ({'Google Doc' if doc.mime_type == GOOGLE_DOC_MIME else 'docx'})"
            if title_grid is None:
                title_grid = extract_labeled_table(document, TITLE_YT_LABEL)
            if desc_grid is None:
                desc_grid = extract_labeled_table(document, DESCRIPTION_LABEL)

        if tn_grid is None:
            tn_missing += 1
            print(f"  TN:     MISSING (no table captioned {TN_LABEL!r})")
        else:
            tn_values = extract_tn_text(tn_grid)
            if not tn_values["english"]:
                tn_empty += 1
                print(f"  TN:     EMPTY table in {tn_source}")
            else:
                tn_ok += 1
                print(f"  TN:     OK in {tn_source}")
                print(f"          EN: {tn_values['english'][:120]}")
                if tn_values["bulgarian"]:
                    print(f"          BG: {tn_values['bulgarian'][:120]}")
                else:
                    print("          BG: (none)")

        ref_bits = []
        if title_grid is not None:
            ref_bits.append("TITLE-YT")
        if desc_grid is not None:
            ref_bits.append("Description")
        if ref_bits:
            print(f"  also:   {', '.join(ref_bits)} table(s) found")
        print()

    print("=== Summary ===")
    print(f"Videos checked:     {len(targets)}")
    print(f"TN text present:    {tn_ok}")
    print(f"TN table empty:     {tn_empty}")
    print(f"TN table missing:   {tn_missing}")
    print(f"No document:        {no_doc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
