from __future__ import annotations

import re
from pathlib import Path


class QuotePdfError(RuntimeError):
    pass


QUOTE_CAPTION_SKIP_PHRASES = (
    "любов и благословии",
    "садгуру българия",
)


def _flatten_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def prepare_quote_caption(text: str) -> str:
    """Normalize quote caption text to one flowing paragraph without PDF/footer noise."""
    flat = _flatten_text(text)
    for phrase in QUOTE_CAPTION_SKIP_PHRASES:
        flat = re.sub(re.escape(phrase), " ", flat, flags=re.IGNORECASE)
    flat = re.sub(r"\s+", " ", flat).strip(" ,")

    sentences: list[str] = []
    seen: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", flat):
        clean = sentence.strip(" ,")
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        sentences.append(clean)
    return " ".join(sentences).strip()


def normalize_extracted_text(text: str) -> str:
    """Collapse PDF extraction noise such as repeated text blocks."""
    paragraphs: list[str] = []
    seen: set[str] = set()
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                block = _flatten_text("\n".join(current))
                if block and block.casefold() not in seen:
                    seen.add(block.casefold())
                    paragraphs.append(block)
                current = []
            continue
        current.append(stripped)
    if current:
        block = _flatten_text("\n".join(current))
        if block and block.casefold() not in seen:
            paragraphs.append(block)
    return "\n\n".join(paragraphs).strip()


def extract_pdf_page_text(pdf_path: Path, page_index: int) -> str:
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise QuotePdfError(
            "PDF caption extraction requires pymupdf. Install it with:\n"
            "  pip install pymupdf"
        ) from exc

    source = pdf_path.resolve()
    if not source.is_file():
        raise QuotePdfError(f"PDF file not found: {source}")
    if page_index < 0:
        raise QuotePdfError(f"PDF page index must be non-negative, got {page_index}")

    try:
        document = fitz.open(source)
        if page_index >= document.page_count:
            raise QuotePdfError(
                f"PDF {source.name} has no page {page_index + 1} "
                f"(page count: {document.page_count})"
            )
        text = document[page_index].get_text().strip()
    except QuotePdfError:
        raise
    except Exception as exc:
        raise QuotePdfError(f"Failed to read PDF {source.name}: {exc}") from exc

    return prepare_quote_caption(normalize_extracted_text(text))


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise QuotePdfError(
            "PDF caption extraction requires pymupdf. Install it with:\n"
            "  pip install pymupdf"
        ) from exc

    source = pdf_path.resolve()
    if not source.is_file():
        raise QuotePdfError(f"PDF file not found: {source}")

    try:
        document = fitz.open(source)
        parts = [
            page.get_text().strip()
            for page in document
            if page.get_text().strip()
        ]
    except Exception as exc:
        raise QuotePdfError(f"Failed to read PDF {source.name}: {exc}") from exc

    text = normalize_extracted_text("\n\n".join(parts))
    return prepare_quote_caption(text)


def _render_pdf_page_to_jpeg(
    pdf_path: Path,
    page_index: int,
    destination: Path,
    *,
    dpi: int = 120,
    max_width: int = 1080,
) -> Path:
    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise QuotePdfError(
            "PDF quote rendering requires pymupdf. Install it with:\n"
            "  pip install pymupdf"
        ) from exc

    source = pdf_path.resolve()
    if not source.is_file():
        raise QuotePdfError(f"PDF file not found: {source}")
    if page_index < 0:
        raise QuotePdfError(f"PDF page index must be non-negative, got {page_index}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_mtime >= source.stat().st_mtime:
        return destination

    try:
        document = fitz.open(source)
        if page_index >= document.page_count:
            raise QuotePdfError(
                f"PDF {source.name} has no page {page_index + 1} "
                f"(page count: {document.page_count})"
            )
        page = document[page_index]
        scale = dpi / 72
        if page.rect.width * scale > max_width:
            scale = max_width / page.rect.width
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        destination.write_bytes(pixmap.tobytes("jpeg", jpg_quality=85))
    except QuotePdfError:
        raise
    except Exception as exc:
        raise QuotePdfError(f"Failed to render PDF {source.name}: {exc}") from exc

    if not destination.is_file():
        raise QuotePdfError(f"PDF render did not create output file: {destination}")
    return destination


def ensure_quote_image_from_pdf_page(
    pdf_path: Path,
    page_index: int,
    work_dir: Path,
    *,
    dpi: int = 120,
    max_width: int = 1080,
) -> Path:
    """Render one PDF page to a cached JPEG for social publishing."""
    source = pdf_path.resolve()
    destination = work_dir / f"{source.stem}-day{page_index + 1:02d}.jpg"
    return _render_pdf_page_to_jpeg(
        source,
        page_index,
        destination,
        dpi=dpi,
        max_width=max_width,
    )


def ensure_quote_image_from_pdf(
    pdf_path: Path,
    work_dir: Path,
    *,
    dpi: int = 120,
    max_width: int = 1080,
) -> Path:
    """Render the first PDF page to a cached JPEG for social publishing."""
    return ensure_quote_image_from_pdf_page(
        pdf_path,
        0,
        work_dir,
        dpi=dpi,
        max_width=max_width,
    )
