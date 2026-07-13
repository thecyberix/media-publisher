from __future__ import annotations

import re
from typing import Any

from catalog_parser.canva import extract_canva_design_url
from media_publisher.sources.canva_share_preview import probe_canva_design_dimensions
from media_publisher.sources.source_thumbnail import (
    SourceThumbnailError,
    aspects_match,
    video_size_from_source_url,
)

CANVA_URL_RE = re.compile(
    r"https?://(?:www\.)?canva\.com/design/[A-Za-z0-9_-]+(?:/[^\s\"'<>]*)?",
    re.IGNORECASE,
)


def dedupe_canva_urls(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        normalized = extract_canva_design_url(raw) or raw.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def collect_canva_urls_from_values(values: list[str | None]) -> list[str]:
    urls: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        direct = extract_canva_design_url(value)
        if direct:
            urls.append(direct)
            continue
        urls.extend(
            match
            for match in (extract_canva_design_url(item) for item in CANVA_URL_RE.findall(value))
            if match
        )
    return dedupe_canva_urls(urls)


def _paragraph_hyperlink_urls(paragraph: Any, qn: Any) -> list[str]:
    urls: list[str] = []
    for hyperlink in paragraph._element.xpath(".//w:hyperlink"):
        relationship_id = hyperlink.get(qn("r:id"))
        if not relationship_id:
            continue
        relationship = paragraph.part.rels.get(relationship_id)
        if relationship is None:
            continue
        target = getattr(relationship, "target_ref", None)
        if isinstance(target, str) and target.strip():
            urls.append(target.strip())
    urls.extend(CANVA_URL_RE.findall(paragraph.text))
    return urls


def extract_canva_links_from_docx(document: Any) -> tuple[list[str], list[str]]:
    from docx.oxml.ns import qn
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    all_urls: list[str] = []
    below_tn_urls: list[str] = []
    blocks: list[tuple[str, Any]] = []

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            blocks.append(("p", Paragraph(child, document)))
        elif isinstance(child, CT_Tbl):
            blocks.append(("t", Table(child, document)))

    for index, (kind, payload) in enumerate(blocks):
        if kind == "p":
            paragraph = payload
            urls = [
                url
                for url in _paragraph_hyperlink_urls(paragraph, qn)
                if extract_canva_design_url(url)
            ]
            all_urls.extend(urls)
            if paragraph.text.strip() == "TN":
                for next_index in range(index + 1, len(blocks)):
                    next_kind, next_payload = blocks[next_index]
                    if next_kind == "t":
                        break
                    next_urls = [
                        url
                        for url in _paragraph_hyperlink_urls(next_payload, qn)
                        if extract_canva_design_url(url)
                    ]
                    below_tn_urls.extend(next_urls)
            continue

        table = payload
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    urls = [
                        url
                        for url in _paragraph_hyperlink_urls(paragraph, qn)
                        if extract_canva_design_url(url)
                    ]
                    all_urls.extend(urls)
                all_urls.extend(
                    match
                    for match in (
                        extract_canva_design_url(item) for item in CANVA_URL_RE.findall(cell.text)
                    )
                    if match
                )

    return dedupe_canva_urls(all_urls), dedupe_canva_urls(below_tn_urls)


def _google_paragraph_text_and_urls(paragraph: dict[str, Any]) -> tuple[str, list[str]]:
    text_parts: list[str] = []
    urls: list[str] = []
    for element in paragraph.get("elements", []):
        if not isinstance(element, dict):
            continue
        text_run = element.get("textRun")
        if not isinstance(text_run, dict):
            continue
        text_parts.append(str(text_run.get("content", "")))
        text_style = text_run.get("textStyle")
        if not isinstance(text_style, dict):
            continue
        link = text_style.get("link")
        if not isinstance(link, dict):
            continue
        url = link.get("url")
        if isinstance(url, str) and url.strip():
            urls.append(url.strip())
    text = "".join(text_parts).strip()
    urls.extend(CANVA_URL_RE.findall(text))
    return text, urls


def extract_canva_links_from_google_document(
    document: dict[str, Any],
) -> tuple[list[str], list[str]]:
    content = document.get("body", {}).get("content", [])
    if not isinstance(content, list):
        return [], []

    from catalog_parser.drive_docs import table_to_grid

    all_urls: list[str] = []
    below_tn_urls: list[str] = []
    blocks: list[tuple[str, Any]] = []

    for element in content:
        if not isinstance(element, dict):
            continue
        if "paragraph" in element:
            paragraph = element["paragraph"]
            if isinstance(paragraph, dict):
                blocks.append(("p", paragraph))
            continue
        table = element.get("table")
        if isinstance(table, dict):
            blocks.append(("t", table_to_grid(table)))

    for index, (kind, payload) in enumerate(blocks):
        if kind == "p":
            text, urls = _google_paragraph_text_and_urls(payload)
            canva_urls = [
                match
                for match in (extract_canva_design_url(url) for url in urls)
                if match
            ]
            all_urls.extend(canva_urls)
            if text == "TN":
                for next_index in range(index + 1, len(blocks)):
                    next_kind, next_payload = blocks[next_index]
                    if next_kind == "t":
                        break
                    next_text, next_urls = _google_paragraph_text_and_urls(next_payload)
                    below_tn_urls.extend(
                        match
                        for match in (extract_canva_design_url(url) for url in next_urls)
                        if match
                    )
            continue

        grid = payload
        for row in grid:
            for cell in row:
                all_urls.extend(
                    match
                    for match in (
                        extract_canva_design_url(item) for item in CANVA_URL_RE.findall(cell)
                    )
                    if match
                )

    return dedupe_canva_urls(all_urls), dedupe_canva_urls(below_tn_urls)


def select_canva_url(
    urls: list[str],
    *,
    target_size: tuple[int, int] | None = None,
    original_video_url: str | None = None,
) -> str | None:
    candidates = dedupe_canva_urls(urls)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    resolved_size = target_size
    if resolved_size is None:
        source_url = str(original_video_url or "").strip()
        if source_url:
            try:
                resolved_size = video_size_from_source_url(source_url)
            except SourceThumbnailError:
                resolved_size = None
    if resolved_size is None:
        return candidates[0]

    dimension_cache: dict[str, tuple[int, int] | None] = {}

    def design_size(url: str) -> tuple[int, int] | None:
        if url not in dimension_cache:
            try:
                dimension_cache[url] = probe_canva_design_dimensions(url)
            except Exception:
                dimension_cache[url] = None
        return dimension_cache[url]

    target_width, target_height = resolved_size
    matching = [
        url
        for url in candidates
        if (size := design_size(url)) is not None
        and aspects_match(target_width, target_height, size[0], size[1])
    ]
    if len(matching) == 1:
        return matching[0]
    if matching:
        return sorted(
            matching,
            key=lambda url: _aspect_distance(resolved_size, design_size(url)),
        )[0]

    sized = [(url, design_size(url)) for url in candidates]
    sized_with_dims = [item for item in sized if item[1] is not None]
    if sized_with_dims:
        return min(
            sized_with_dims,
            key=lambda item: _aspect_distance(resolved_size, item[1]),
        )[0]
    return candidates[0]


def _aspect_distance(
    target_size: tuple[int, int],
    candidate_size: tuple[int, int] | None,
) -> float:
    if candidate_size is None:
        return float("inf")
    target_width, target_height = target_size
    candidate_width, candidate_height = candidate_size
    if target_height <= 0 or candidate_height <= 0:
        return float("inf")
    return abs((target_width / target_height) - (candidate_width / candidate_height))
