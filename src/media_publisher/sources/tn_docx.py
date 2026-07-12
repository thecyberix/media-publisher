from __future__ import annotations

import io
import re
from typing import Iterator

from docx import Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

from media_publisher.sources.google_drive import GoogleDriveClient

WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
TN_LABEL = "TN"


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def docx_table_to_grid(table: Table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


def iter_docx_blocks(document: Document) -> Iterator[tuple[str, object]]:
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


def normalize_psd_text(text: str) -> str:
    cleaned = text.replace("\x03", "\n").replace("\r", "\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def english_lines_for_render(english: str) -> list[str]:
    parts = [part.strip() for part in english.splitlines() if part.strip()]
    if parts:
        return parts
    compact = re.sub(r"\s+", " ", english.strip())
    return [compact] if compact else []


def caption_lines_for_render(caption: str) -> list[str]:
    """Split translated TN caption text on newlines or `` / `` markers."""
    normalized = caption.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []
    newline_parts = [part.strip() for part in normalized.splitlines() if part.strip()]
    if len(newline_parts) > 1:
        return newline_parts
    slash_parts = [part.strip() for part in re.split(r"\s*/\s*", normalized) if part.strip()]
    if len(slash_parts) > 1:
        return slash_parts
    if newline_parts:
        return newline_parts
    compact = re.sub(r"\s+", " ", normalized)
    return [compact] if compact else []
