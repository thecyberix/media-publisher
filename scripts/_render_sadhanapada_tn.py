"""One-off Sadhanapada TN render from Airtable Original Video Thumbnail."""
from __future__ import annotations

import sys
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PIL import Image, ImageDraw, ImageFilter

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.tn_psd import TnLineStyle, safe_cache_name
from media_publisher.sources.tn_renderer import render_tn_thumbnail

RECORD_ID = "recvLbdtbzZE3A4gR"
CAPTION_LINES = (
    "Саданапада",
    "Открийте",
    "Аромата",
    "на Живота",
)
OUTPUT_DIR = PROJECT_ROOT / "downloads" / "tn-rendered"
TITLE_COLOR = "#FEEEA2"


def download_airtable_thumbnail(fields: dict, destination: Path) -> None:
    attachment = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    if not isinstance(attachment, list) or not attachment:
        raise RuntimeError("Original Video Thumbnail is missing in Airtable")
    first = attachment[0]
    if not isinstance(first, dict):
        raise RuntimeError("Original Video Thumbnail attachment is invalid")
    url = str(first.get("url") or "").strip()
    if not url:
        raise RuntimeError("Original Video Thumbnail attachment has no URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    destination.write_bytes(response.content)


def pdf_line_boxes(pdf_path: Path, image_size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    page = document[0]
    page_width, page_height = page.get_width(), page.get_height()
    scale_x = image_size[0] / page_width
    scale_y = image_size[1] / page_height
    textpage = page.get_textpage()

    def to_image_box(start: int, end: int) -> tuple[int, int, int, int] | None:
        boxes = [
            textpage.get_charbox(index)
            for index in range(start, end)
            if textpage.get_text_range(index, index + 1) not in "\r\n"
        ]
        if not boxes:
            return None
        xs = [value for box in boxes for value in (box[0], box[2])]
        ys = [value for box in boxes for value in (box[1], box[3])]
        left = round(min(xs) * scale_x)
        right = round(max(xs) * scale_x)
        top = round((page_height - max(ys)) * scale_y)
        bottom = round((page_height - min(ys)) * scale_y)
        return (left, top, right, bottom)

    first = textpage.search("Sadhanapada").get_next()
    if first is None:
        raise RuntimeError("Could not locate Sadhanapada text box in Drive TN PDF")

    start, end = first
    anchor = to_image_box(start, end)
    if anchor is None:
        raise RuntimeError("Could not derive Sadhanapada text box from PDF")

    left, top, right, bottom = anchor
    line_height = bottom - top
    gap = max(6, round(line_height * 0.08))
    boxes: list[tuple[int, int, int, int]] = []
    for index in range(len(CAPTION_LINES)):
        line_top = top + index * (line_height + gap)
        line_bottom = line_top + line_height
        boxes.append((left, line_top, right, line_bottom))
    return boxes


def pdf_text_cover_bounds(
    pdf_path: Path,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(str(pdf_path))
    page = document[0]
    page_width, page_height = page.get_width(), page.get_height()
    scale_x = image_size[0] / page_width
    scale_y = image_size[1] / page_height
    textpage = page.get_textpage()
    xs: list[float] = []
    ys_top: list[float] = []
    ys_bottom: list[float] = []
    for index in range(textpage.count_chars()):
        if textpage.get_text_range(index, index + 1) in "\r\n":
            continue
        box = textpage.get_charbox(index)
        xs.extend((box[0], box[2]))
        ys_top.append((page_height - box[3]) * scale_y)
        ys_bottom.append((page_height - box[1]) * scale_y)
    if not xs:
        raise RuntimeError("Could not derive PDF text bounds")
    text_left = max(0, round(min(xs) * scale_x) - 16)
    text_top = max(0, round(min(ys_top)) - 32)
    text_bottom = min(image_size[1], round(max(ys_bottom)) + 140)
    cover_right = min(image_size[0], round(max(xs) * scale_x) + 24)
    return (text_left, text_top, cover_right, text_bottom)


def rendered_text_bounds(line_styles: list[TnLineStyle]) -> tuple[int, int, int, int]:
    from media_publisher.sources.quote_renderer import _measure_text, load_font, resolve_font_path
    from media_publisher.sources.tn_renderer import TN_FONT_BOLD_CANDIDATES

    font_path = resolve_font_path(TN_FONT_BOLD_CANDIDATES[0])
    left = min(style.bbox[0] for style in line_styles)
    top = min(style.bbox[1] for style in line_styles)
    bottom = max(style.bbox[3] for style in line_styles)
    max_width = 0
    for style in line_styles:
        size = int(style.fixed_font_size_px or style.font_size_px)
        font = load_font(size, font_path=font_path)
        line_width, _line_height = _measure_text(font, style.rendered_text)
        max_width = max(max_width, line_width)
    return (left, top, left + max_width, bottom)


def reposition_cover_on_text(
    cover_bounds: tuple[int, int, int, int],
    text_bounds: tuple[int, int, int, int],
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    cover_left, cover_top, cover_right, cover_bottom = cover_bounds
    panel_width = cover_right - cover_left
    panel_height = cover_bottom - cover_top
    text_left, text_top, text_right, text_bottom = text_bounds
    text_center_x = (text_left + text_right) / 2
    text_center_y = (text_top + text_bottom) / 2
    new_left = round(text_center_x - panel_width / 2)
    new_top = round(text_center_y - panel_height / 2)
    new_right = new_left + panel_width
    new_bottom = new_top + panel_height

    image_width, image_height = image_size
    if new_left < 0:
        new_right -= new_left
        new_left = 0
    if new_top < 0:
        new_bottom -= new_top
        new_top = 0
    if new_right > image_width:
        shift = new_right - image_width
        new_left -= shift
        new_right = image_width
    if new_bottom > image_height:
        shift = new_bottom - image_height
        new_top -= shift
        new_bottom = image_height
    return (new_left, new_top, new_right, new_bottom)


def cover_text_region(
    image: Image.Image,
    cover_bounds: tuple[int, int, int, int],
    *,
    solid_fill: bool = True,
) -> Image.Image:
    left, top, right, bottom = cover_bounds
    result = image.copy()
    region = image.crop((left, top, right, bottom))
    covered = region.filter(ImageFilter.GaussianBlur(radius=20))
    result.paste(covered, (left, top))
    if not solid_fill:
        return result
    sample = image.getpixel((max(0, left + 8), min(image.height - 1, top + 12)))
    if not isinstance(sample, tuple):
        sample = (sample, sample, sample)
    draw = ImageDraw.Draw(result)
    draw.rectangle((left, top, right, bottom), fill=sample[:3])
    return result


BODY_COLORS = ("#FFFFFF", "#FFFFFE", "#FFFEFF")


def sadhanapada_line_styles(
    boxes: list[tuple[int, int, int, int]],
    cover_bounds: tuple[int, int, int, int],
) -> list[TnLineStyle]:
    left, _top, cover_right, _bottom = cover_bounds
    text_right = cover_right - 12
    line_height = boxes[0][3] - boxes[0][1]
    title_size = round(max(52.0, line_height * 1.22))
    body_size = round(max(48.0, line_height * 1.12))
    styles: list[TnLineStyle] = []
    for index, (line, (_left, top, _right, bottom)) in enumerate(
        zip(CAPTION_LINES, boxes, strict=True)
    ):
        styles.append(
            TnLineStyle(
                placeholder_text=line,
                rendered_text=line,
                bbox=(left, top, text_right, bottom),
                font_size_px=float(title_size if index == 0 else body_size),
                color_hex=TITLE_COLOR if index == 0 else BODY_COLORS[index - 1],
                layer_name=f"sadhanapada-{index + 1}",
                alignment="left",
                fixed_font_size_px=title_size if index == 0 else body_size,
                max_grow_factor=1.0,
            )
        )
    return styles


def main() -> int:
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    record = airtable.get_record(RECORD_ID)
    fields = record.fields
    title = catalog_title(fields)

    cache_dir = PROJECT_ROOT / settings.tn_cache_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    airtable_path = cache_dir / f"{safe_cache_name(title)}.airtable-tn.jpg"
    download_airtable_thumbnail(fields, airtable_path)

    from media_publisher.sources.google_drive import GoogleDriveClient

    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    folder_id = "1hMttBGf1xHs1Wwl7LwYvpe-H14iZbBsP"
    pdf_item = next(
        item for item in drive.list_children(folder_id) if item.mime_type == "application/pdf"
    )
    pdf_path = cache_dir / safe_cache_name(pdf_item.name)
    if not pdf_path.is_file():
        drive.download_file(pdf_item.id, pdf_path)

    template = Image.open(airtable_path).convert("RGB")
    boxes = pdf_line_boxes(pdf_path, template.size)
    cover_bounds = pdf_text_cover_bounds(pdf_path, template.size)
    line_styles = sadhanapada_line_styles(boxes, cover_bounds)
    text_bounds = rendered_text_bounds(line_styles)
    display_cover_bounds = reposition_cover_on_text(
        cover_bounds, text_bounds, template.size
    )
    template = cover_text_region(template, cover_bounds, solid_fill=False)
    template = cover_text_region(template, display_cover_bounds)
    caption_text = "\n".join(CAPTION_LINES)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cleaned = title.replace(":", "_").replace("?", "_").replace("|", "_")
    destination = OUTPUT_DIR / f"{cleaned}.tn-render.jpg"

    result = render_tn_thumbnail(
        template=template,
        english_text=caption_text,
        line_styles=line_styles,
        destination=destination,
        catalog_title=title,
    )
    print(f"Source: Airtable Original Video Thumbnail ({airtable_path.name})")
    print(f"Rendered {result.width}x{result.height}, {result.line_count} line(s)")
    print(f"Saved: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
