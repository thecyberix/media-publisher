"""Cross-check SM catalog unpublished videos for Drive images and Canva links."""
from __future__ import annotations

import io
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse, urlunparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src/catalog_parser"))

from catalog_parser.parser import rows_to_records
from docx import Document
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from google.oauth2 import service_account
from googleapiclient.discovery import build

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
)

FIELD_ORIGINAL_VIDEO_NAME = "Original Video Name"
SHEET_ID = "1BGxTfnvs3zezyJVTSXroy9N0l7j5QHbzPzRj_TSjO-c"
SHEET_TAB = "English"
TN_FIELD = "pkgTn"
STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)
ORIGINAL_VIDEO_NAME_SUFFIX = " | Sadhguru"
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
CANVA_RE = re.compile(r"https?://(?:www\.)?(?:canva\.com|canva\.link)[^\s\"']*", re.I)
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd",
}
PDF_MIME = "application/pdf"
THUMBNAIL_FILE_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}


def is_thumbnail_file(name: str, mime_type: str) -> bool:
    if mime_type == PDF_MIME:
        return True
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in THUMBNAIL_FILE_EXTENSIONS


def normalize_title(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(ORIGINAL_VIDEO_NAME_SUFFIX):
        text = text[: -len(ORIGINAL_VIDEO_NAME_SUFFIX)].rstrip()
    return " ".join(text.casefold().split()) or None


def normalize_url(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = urlparse(text)
    if not parsed.scheme:
        return text.casefold()
    return urlunparse(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            "",
            parsed.query,
            "",
        )
    )


def status_bucket(status: object) -> str | None:
    text = str(status or "")
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    return f"AND(OR({', '.join(clauses)}), {{Type}} != \"{TYPE_QUOTE}\")"


def tn_is_marked(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text) and text.upper() != "X"


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


def fetch_catalog_records() -> list[dict]:
    creds = service_account.Credentials.from_service_account_file(
        str(PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    values = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range=SHEET_TAB)
        .execute()
        .get("values", [])
    )
    return rows_to_records(values[0], values[1:]) if values else []


def index_catalog(records: list[dict]) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    by_url: dict[str, list[dict]] = defaultdict(list)
    by_title: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if not record.get("pkgSmLk"):
            continue
        url_key = normalize_url(record.get("ctLink"))
        if url_key:
            by_url[url_key].append(record)
        title_key = normalize_title(record.get("ctTitle"))
        if title_key:
            by_title[title_key].append(record)
    return by_url, by_title


def match_catalog_row(fields: dict, by_url, by_title) -> dict | None:
    for candidate in (fields.get(FIELD_ORIGINAL_VIDEO), fields.get("Original Video")):
        url_key = normalize_url(candidate)
        if url_key and by_url.get(url_key):
            return by_url[url_key][0]
    for candidate in (
        fields.get(FIELD_ORIGINAL_VIDEO_NAME),
        fields.get(FIELD_TITLE),
        fields.get("Title"),
    ):
        title_key = normalize_title(candidate)
        if title_key and by_title.get(title_key):
            return by_title[title_key][0]
    return None


def read_word_document(drive, doc) -> Document | None:
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


def paragraph_urls(paragraph: Paragraph) -> list[str]:
    urls: list[str] = []
    for hyperlink in paragraph._element.xpath(".//w:hyperlink"):
        rel_id = hyperlink.get(qn("r:id"))
        if rel_id and rel_id in paragraph.part.rels:
            urls.append(paragraph.part.rels[rel_id].target_ref)
    urls.extend(CANVA_RE.findall(paragraph.text))
    return urls


def extract_canva_links(document: Document) -> tuple[list[str], list[str]]:
    all_urls: list[str] = []
    below_tn_urls: list[str] = []
    blocks: list[tuple[str, object]] = []
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(("p", Paragraph(child, document)))
        elif isinstance(child, CT_Tbl):
            blocks.append(("t", docx_table_to_grid(Table(child, document))))

    for i, (kind, payload) in enumerate(blocks):
        if kind == "p":
            urls = [u for u in paragraph_urls(payload) if CANVA_RE.search(u)]
            all_urls.extend(urls)
            if payload.text.strip() == "TN":
                for j in range(i + 1, len(blocks)):
                    next_kind, next_payload = blocks[j]
                    if next_kind == "t":
                        break
                    urls = [u for u in paragraph_urls(next_payload) if CANVA_RE.search(u)]
                    below_tn_urls.extend(urls)
        elif kind == "t":
            for row in payload:
                for cell in row:
                    all_urls.extend(CANVA_RE.findall(cell))

    return list(dict.fromkeys(all_urls)), list(dict.fromkeys(below_tn_urls))


def docx_table_to_grid(table: Table) -> list[list[str]]:
    return [[cell.text.strip() for cell in row.cells] for row in table.rows]


def document_sort_key(item) -> tuple[int, str]:
    return (0 if item.name.upper().startswith("TEXT_") else 1, item.name.casefold())


def main() -> int:
    import argparse

    from media_publisher.sources.google_drive import GoogleDriveClient

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--unmarked-only",
        action="store_true",
        help="Check videos matched in SM catalog but with pkgTn empty or X",
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

    catalog = fetch_catalog_records()
    by_url, by_title = index_catalog(catalog)
    folder_cache: dict[str, list] = {}
    doc_cache: dict[str, tuple[list[str], list[str]]] = {}

    targets: list[dict] = []
    for record in airtable.list_records(filter_formula=build_filter_formula()):
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        sheet_row = match_catalog_row(fields, by_url, by_title)
        if sheet_row is None:
            continue
        marked = tn_is_marked(sheet_row.get(TN_FIELD))
        if args.unmarked_only:
            if marked:
                continue
        elif not marked:
            continue
        targets.append(
            {
                "title": str(
                    fields.get(FIELD_ORIGINAL_VIDEO_NAME)
                    or fields.get(FIELD_TITLE)
                    or "Untitled"
                ).strip(),
                "status": bucket,
                "folder_id": parse_folder_id(fields.get(FIELD_VIDEO_FOLDER)),
            }
        )

    label = "unmarked" if args.unmarked_only else "pkgTn-marked"
    print(f"=== Drive assets for {len(targets)} {label} unpublished videos ===")
    print()
    print("Thumbnail asset paths counted:")
    print("  1. Root PSD/JPG/PNG in Video Folder")
    print("  2. Root PDF in Video Folder")
    print("  3. Canva link in TEXT_ doc (usually below TN caption)")
    print()

    summary = Counter()
    file_kind_totals = Counter()
    rows: list[dict] = []

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        folder_id = item["folder_id"]
        images: list[str] = []
        image_kinds: list[str] = []
        canva_any: list[str] = []
        canva_below_tn: list[str] = []
        doc_name: str | None = None

        if folder_id is None:
            summary["no_folder"] += 1
        else:
            if folder_id not in folder_cache:
                folder_cache[folder_id] = drive.list_children(folder_id)
            children = folder_cache[folder_id]
            images = [
                child.name
                for child in children
                if is_thumbnail_file(child.name, child.mime_type)
            ]
            image_kinds = []
            for child in children:
                if not is_thumbnail_file(child.name, child.mime_type):
                    continue
                if child.mime_type == PDF_MIME or child.name.casefold().endswith(".pdf"):
                    image_kinds.append("pdf")
                elif child.name.casefold().endswith(".psd") or "photoshop" in child.mime_type.casefold():
                    image_kinds.append("psd")
                else:
                    image_kinds.append("image")
            if images:
                summary["has_image"] += 1
                for kind in set(image_kinds):
                    file_kind_totals[kind] += 1
            else:
                summary["no_image"] += 1

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
                doc_name = doc.name
                if doc.id not in doc_cache:
                    document = read_word_document(drive, doc)
                    doc_cache[doc.id] = (
                        extract_canva_links(document) if document else ([], [])
                    )
                canva_any, canva_below_tn = doc_cache[doc.id]

            if canva_any:
                summary["has_canva"] += 1
            else:
                summary["no_canva"] += 1
            if canva_below_tn:
                summary["canva_below_tn"] += 1

            if images and canva_any:
                summary["image_and_canva"] += 1
            elif images:
                summary["file_only"] += 1
            elif canva_any:
                summary["canva_only"] += 1
            else:
                summary["neither"] += 1

        rows.append(
            {
                **item,
                "images": images,
                "image_kinds": image_kinds,
                "canva_any": canva_any,
                "canva_below_tn": canva_below_tn,
                "doc_name": doc_name,
            }
        )

    print("=== Summary ===")
    print(f"Videos checked:        {len(targets)}")
    print(f"No Video Folder:       {summary['no_folder']}")
    print(f"Root thumbnail file:   {summary['has_image']} (psd/image/pdf)")
    print(f"  PSD:                 {file_kind_totals['psd']}")
    print(f"  raster image:        {file_kind_totals['image']}")
    print(f"  PDF:                 {file_kind_totals['pdf']}")
    print(f"No root thumbnail:     {summary['no_image']}")
    print(f"Canva link in TEXT doc:{summary['has_canva']}")
    print(f"Canva below TN caption:{summary['canva_below_tn']}")
    print()
    print("Asset combos (with folder):")
    print(f"  file + Canva:        {summary['image_and_canva']}")
    print(f"  file only (psd/pdf/image): {summary['file_only']}")
    print(f"  Canva only:          {summary['canva_only']}")
    print(f"  neither:             {summary['neither']}")
    print()

    for row in rows:
        print(row["title"])
        print(f"  status: {row['status']}")
        if row["folder_id"] is None:
            print("  folder: missing")
            print()
            continue
        if row["images"]:
            names = ", ".join(row["images"][:3])
            if len(row["images"]) > 3:
                names += f" (+{len(row['images']) - 3} more)"
            kinds = row.get("image_kinds") or []
            kind_note = f" [{', '.join(sorted(set(kinds)))}]" if kinds else ""
            print(f"  files:  {names}{kind_note}")
        else:
            print("  files:  none")
        if row["doc_name"]:
            print(f"  doc:    {row['doc_name']}")
        if row["canva_below_tn"]:
            print(f"  canva (below TN): {row['canva_below_tn'][0]}")
        elif row["canva_any"]:
            print(f"  canva (in doc):   {row['canva_any'][0]}")
        else:
            print("  canva:  none")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
