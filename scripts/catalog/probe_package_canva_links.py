"""Probe Video Folder docs for Canva links (field-code aware).

Usage:
  python scripts/catalog/probe_package_canva_links.py --workflow-statuses
  python scripts/catalog/probe_package_canva_links.py --title "Ancient Hindu..."
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
from dataclasses import dataclass, field
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
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    WORKFLOW_STATUSES,
)
from catalog_parser.auth import get_docs_service, get_drive_service_noninteractive
from catalog_parser.canva import extract_canva_design_url
from catalog_parser.canva_selection import (
    CANVA_URL_RE,
    HYPERLINK_FIELD_RE,
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

CANVA_ANY_RE = re.compile(
    r"https?://[^\s\"'<>]*canva[^\s\"'<>]*",
    re.IGNORECASE,
)


@dataclass
class DocScan:
    name: str
    mime: str
    canva_urls: list[str] = field(default_factory=list)
    field_code_canva_urls: list[str] = field(default_factory=list)
    modern_hyperlink_canva_urls: list[str] = field(default_factory=list)
    plain_text_canva_urls: list[str] = field(default_factory=list)
    other_canva_like: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class RecordScan:
    record_id: str
    title: str
    status: str
    video_type: str
    has_thumb: bool
    folder_link: str | None
    docs: list[DocScan] = field(default_factory=list)
    skip_reason: str | None = None

    @property
    def all_canva_urls(self) -> list[str]:
        urls: list[str] = []
        seen: set[str] = set()
        for doc in self.docs:
            for url in doc.canva_urls:
                if url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls

    @property
    def field_code_only_urls(self) -> list[str]:
        """Canva URLs found via field codes but not via w:hyperlink / plain text."""
        modern: set[str] = set()
        field_code: set[str] = set()
        for doc in self.docs:
            modern.update(doc.modern_hyperlink_canva_urls)
            modern.update(doc.plain_text_canva_urls)
            field_code.update(doc.field_code_canva_urls)
        return sorted(field_code - modern)


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


def _normalize_canva(urls: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        normalized = extract_canva_design_url(raw) or ""
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _classify_docx_canva(docx_document: Document) -> DocScan:
    from docx.oxml.ns import qn
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    modern: list[str] = []
    plain: list[str] = []
    field_code: list[str] = []

    def paragraph_sources(paragraph: Paragraph) -> None:
        for hyperlink in paragraph._element.xpath(".//w:hyperlink"):
            rid = hyperlink.get(qn("r:id"))
            if not rid:
                continue
            rel = paragraph.part.rels.get(rid)
            target = getattr(rel, "target_ref", None) if rel is not None else None
            if isinstance(target, str) and extract_canva_design_url(target):
                modern.append(target)
        plain.extend(CANVA_URL_RE.findall(paragraph.text or ""))

    for child in docx_document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph_sources(Paragraph(child, docx_document))
        elif isinstance(child, CT_Tbl):
            table = Table(child, docx_document)
            visited: set[int] = set()
            for row in table.rows:
                for cell in row.cells:
                    tc_id = id(cell._tc)
                    if tc_id in visited:
                        continue
                    visited.add(tc_id)
                    for paragraph in cell.paragraphs:
                        paragraph_sources(paragraph)
                    plain.extend(CANVA_URL_RE.findall(cell.text or ""))

    instr_chunks: list[str] = []
    for node in docx_document.element.body.xpath(".//*[local-name()='instrText']"):
        text = getattr(node, "text", None)
        if isinstance(text, str) and text:
            instr_chunks.append(text)
    instr_joined = "".join(instr_chunks)
    for match in HYPERLINK_FIELD_RE.finditer(instr_joined):
        candidate = match.group(1) or match.group(2)
        if candidate and extract_canva_design_url(candidate):
            field_code.append(candidate)
    field_code.extend(CANVA_URL_RE.findall(instr_joined))

    body_xml = docx_document.element.body.xml
    other_canva_like = sorted(
        {
            hit
            for hit in CANVA_ANY_RE.findall(body_xml)
            if not extract_canva_design_url(hit)
        }
    )

    canva_urls, _below = extract_canva_links_from_docx(docx_document)
    return DocScan(
        name="",
        mime=WORD_DOC_MIME_TYPE,
        canva_urls=canva_urls,
        field_code_canva_urls=_normalize_canva(field_code),
        modern_hyperlink_canva_urls=_normalize_canva(modern),
        plain_text_canva_urls=_normalize_canva(plain),
        other_canva_like=other_canva_like,
    )


def _scan_folder(
    drive_service,
    docs_service,
    folder_id: str,
) -> list[DocScan]:
    results: list[DocScan] = []
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
                scan = _classify_docx_canva(docx_document)
                scan.name = name
                scan.mime = mime_type
                results.append(scan)
            elif mime_type == GOOGLE_DOC_MIME_TYPE and docs_service is not None:
                google_document = (
                    docs_service.documents().get(documentId=document_id).execute()
                )
                if not isinstance(google_document, dict):
                    continue
                urls, _below = extract_canva_links_from_google_document(google_document)
                results.append(
                    DocScan(
                        name=name,
                        mime=mime_type,
                        canva_urls=urls,
                        modern_hyperlink_canva_urls=urls,
                    )
                )
        except Exception as exc:
            results.append(
                DocScan(name=name, mime=mime_type or "", error=str(exc))
            )
    return results


def _scan_record(drive_service, docs_service, record: dict) -> RecordScan:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else {}
    title = str(fields.get(FIELD_TITLE) or record.get("id") or "?")
    status = str(fields.get(FIELD_STATUS) or "")
    video_type = str(fields.get(FIELD_TYPE) or "")
    has_thumb = has_original_video_thumbnail(fields)
    folder_link = fields.get(FIELD_VIDEO_FOLDER)
    result = RecordScan(
        record_id=str(record.get("id") or ""),
        title=title,
        status=status,
        video_type=video_type,
        has_thumb=has_thumb,
        folder_link=folder_link if isinstance(folder_link, str) else None,
    )
    if not isinstance(folder_link, str) or not folder_link.strip():
        result.skip_reason = "missing Video Folder"
        return result
    folder_id = extract_drive_folder_id(folder_link)
    if folder_id is None:
        result.skip_reason = "unparseable Video Folder"
        return result
    result.docs = _scan_folder(drive_service, docs_service, folder_id)
    if not result.docs:
        result.skip_reason = "no Word/Google docs"
    return result


def _workflow_status_formula() -> str:
    clauses = [f'{{Status}}="{status}"' for status in WORKFLOW_STATUSES]
    return f"OR({', '.join(clauses)})"


def _find_records_by_titles(airtable: AirtableClient, titles: list[str]) -> list[dict]:
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
    for key, title in wanted.items():
        if key not in found:
            print(f"MISSING Airtable row: {title}")
    return [found[title.casefold()] for title in titles if title.casefold() in found]


def _print_summary(scans: list[RecordScan]) -> None:
    checked = [s for s in scans if not s.skip_reason]
    with_canva = [s for s in checked if s.all_canva_urls]
    missing_thumb_with_canva = [s for s in with_canva if not s.has_thumb]
    field_code_hits = [s for s in checked if s.field_code_only_urls]
    other_canva_like = [
        s
        for s in checked
        if any(doc.other_canva_like for doc in s.docs) and not s.all_canva_urls
    ]
    errors = [s for s in scans if any(doc.error for doc in s.docs)]

    print("\n========== SUMMARY ==========")
    print(f"Records scanned: {len(scans)}")
    print(f"Folders with docs checked: {len(checked)}")
    print(f"With detectable canva.com/design link: {len(with_canva)}")
    print(
        f"Missing Original Video Thumbnail but DOCX/GDoc has Canva: "
        f"{len(missing_thumb_with_canva)}"
    )
    print(
        f"Canva only via Word field-code HYPERLINK (old extractor miss): "
        f"{len(field_code_hits)}"
    )
    print(
        f"Has canva-like URL in DOCX XML but not canva.com/design: "
        f"{len(other_canva_like)}"
    )
    print(f"Doc scan errors: {len(errors)}")

    if field_code_hits:
        print("\n--- Field-code-only Canva (would have been missed before) ---")
        for scan in field_code_hits:
            print(
                f"- [{scan.status}] {scan.title} "
                f"thumb={'yes' if scan.has_thumb else 'NO'}"
            )
            for url in scan.field_code_only_urls:
                print(f"    {url}")

    if missing_thumb_with_canva:
        print("\n--- Missing thumbnail but Canva present in package doc ---")
        for scan in missing_thumb_with_canva:
            sources = []
            for doc in scan.docs:
                if doc.field_code_canva_urls and not (
                    doc.modern_hyperlink_canva_urls or doc.plain_text_canva_urls
                ):
                    sources.append("field-code")
                elif doc.canva_urls:
                    sources.append("hyperlink/text")
            print(
                f"- [{scan.status}/{scan.video_type}] {scan.title} "
                f"({','.join(sorted(set(sources))) or 'unknown'})"
            )
            for url in scan.all_canva_urls:
                print(f"    {url}")

    if other_canva_like:
        print("\n--- canva-like URLs that are NOT canva.com/design ---")
        for scan in other_canva_like:
            print(f"- [{scan.status}] {scan.title}")
            for doc in scan.docs:
                for hit in doc.other_canva_like:
                    print(f"    {hit}")


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", action="append", default=[], help="Exact Airtable Title")
    parser.add_argument(
        "--today-aspect-mismatch",
        action="store_true",
        help="Probe titles from 2026-09-07 aspect-mismatch ingest misses",
    )
    parser.add_argument(
        "--workflow-statuses",
        action="store_true",
        help=(
            "Scan all records in To do / Translation done / Editing done / "
            "Synchronization done"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-record details (default: summary + notable hits only)",
    )
    args = parser.parse_args()
    titles = list(args.title)
    if args.today_aspect_mismatch:
        titles.extend(TODAY_ASPECT_MISMATCH_TITLES)
    if not titles and not args.workflow_statuses:
        parser.error("Provide --workflow-statuses and/or --title / --today-aspect-mismatch")

    airtable = _airtable_client()
    drive = get_drive_service_noninteractive()
    docs = get_docs_service(Path("credentials.json"), Path("token.json"))

    records: list[dict] = []
    if args.workflow_statuses:
        records = airtable.list_records(filter_formula=_workflow_status_formula())
        print(f"Loaded {len(records)} workflow-status record(s)")
    if titles:
        titled = _find_records_by_titles(airtable, titles)
        by_id = {r.get("id"): r for r in records}
        for record in titled:
            rid = record.get("id")
            if rid not in by_id:
                records.append(record)

    scans: list[RecordScan] = []
    for index, record in enumerate(records, start=1):
        scan = _scan_record(drive, docs, record)
        scans.append(scan)
        if args.verbose or scan.field_code_only_urls or (
            scan.all_canva_urls and not scan.has_thumb
        ) or any(doc.other_canva_like for doc in scan.docs if not scan.all_canva_urls):
            print(
                f"\n[{index}/{len(records)}] {scan.status} | {scan.title} "
                f"| thumb={'yes' if scan.has_thumb else 'NO'}"
            )
            if scan.skip_reason:
                print(f"  skip: {scan.skip_reason}")
                continue
            for doc in scan.docs:
                if doc.error:
                    print(f"  WARN {doc.name}: {doc.error}")
                    continue
                kind = "docx" if doc.mime == WORD_DOC_MIME_TYPE else "gdoc"
                if doc.canva_urls:
                    print(f"  [{kind}] {doc.name}: {len(doc.canva_urls)} Canva link(s)")
                    if doc.field_code_canva_urls:
                        print(f"    field-code: {doc.field_code_canva_urls}")
                    if doc.modern_hyperlink_canva_urls:
                        print(f"    w:hyperlink: {doc.modern_hyperlink_canva_urls}")
                    if doc.plain_text_canva_urls:
                        print(f"    plain text: {doc.plain_text_canva_urls}")
                else:
                    print(f"  [{kind}] {doc.name}: no canva.com/design link")
                if doc.other_canva_like:
                    print(f"    other canva-like: {doc.other_canva_like}")
        elif index % 25 == 0:
            print(f"... scanned {index}/{len(records)}")

    _print_summary(scans)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
