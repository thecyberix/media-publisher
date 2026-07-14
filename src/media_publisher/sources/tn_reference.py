"""Derive TN line styles from an original-platform reference thumbnail."""
from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("TN reference helpers require Pillow") from exc

from media_publisher.sources.tn_psd import TnLineStyle

TEXT_LUMINANCE_THRESHOLD = 180
TEXT_ROW_PIXEL_RATIO = 0.02
SCAN_START_RATIO = 0.55
MIN_BAND_HEIGHT_RATIO = 0.008
REFERENCE_FONT_SCALE = 1.35
REFERENCE_BOX_HEIGHT_SCALE = 1.28
REFERENCE_MAX_GROW_FACTOR = 1.6
REFERENCE_BODY_LINE_GAP_FACTOR = 0.05
REFERENCE_LABEL_FONT_RATIO = 0.70
REFERENCE_LABEL_GAP_FACTOR = 0.10
REFERENCE_LABEL_BOX_WIDTH_RATIO = 0.4125
REFERENCE_LABEL_BOX_HEIGHT_FONT_FACTOR = 0.9072
REFERENCE_LABEL_BOX_HEIGHT_WIDTH_RATIO = 0.3888


def _is_text_pixel(red: int, green: int, blue: int) -> bool:
    luminance = (red + green + blue) / 3
    if luminance >= TEXT_LUMINANCE_THRESHOLD:
        return True
    return red > 160 and green > 120 and blue < red - 40


def _hex_from_rgb(red: int, green: int, blue: int) -> str:
    return f"#{red:02X}{green:02X}{blue:02X}"


def _average_rgb(pixels: list[tuple[int, int, int]]) -> tuple[int, int, int]:
    if not pixels:
        return (255, 255, 255)
    total = [0, 0, 0]
    for red, green, blue in pixels:
        total[0] += red
        total[1] += green
        total[2] += blue
    count = len(pixels)
    return (total[0] // count, total[1] // count, total[2] // count)


def _resize_reference(reference: Image.Image, template_size: tuple[int, int]) -> Image.Image:
    if reference.size == template_size:
        return reference
    return reference.resize(template_size, Image.Resampling.LANCZOS)


def _text_bands(
    reference: Image.Image,
    *,
    scan_start_ratio: float = SCAN_START_RATIO,
) -> list[tuple[int, int, int, int, str, str | None]]:
    width, height = reference.size
    pixels = reference.load()
    y_start = int(height * scan_start_ratio)
    row_threshold = max(1, int(width * TEXT_ROW_PIXEL_RATIO))

    bands: list[tuple[int, int, int, int]] = []
    in_band = False
    band_start = 0
    for y in range(y_start, height):
        bright_count = 0
        for x in range(width):
            red, green, blue = pixels[x, y][:3]
            if _is_text_pixel(red, green, blue):
                bright_count += 1
        if bright_count > row_threshold and not in_band:
            in_band = True
            band_start = y
        elif bright_count <= row_threshold and in_band:
            in_band = False
            bands.append((0, band_start, width, y))
    if in_band:
        bands.append((0, band_start, width, height))

    styled: list[tuple[int, int, int, int, str, str | None]] = []
    for _left, top, _right, bottom in bands:
        text_pixels: list[tuple[int, int, int]] = []
        background_pixels: list[tuple[int, int, int]] = []
        min_x = width
        max_x = 0
        for y in range(top, bottom):
            for x in range(width):
                red, green, blue = pixels[x, y][:3]
                if _is_text_pixel(red, green, blue):
                    text_pixels.append((red, green, blue))
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
                elif blue > red + 20 and blue > 90:
                    background_pixels.append((red, green, blue))

        if min_x >= max_x:
            continue

        margin_x = max(8, int(width * 0.04))
        left = max(0, min_x - margin_x)
        right = min(width, max_x + margin_x)
        text_color = _average_rgb(text_pixels)
        background_hex: str | None = None
        if background_pixels:
            background = _average_rgb(background_pixels)
            background_hex = _hex_from_rgb(*background)
        styled.append(
            (
                left,
                top,
                right,
                bottom,
                _hex_from_rgb(*text_color),
                background_hex,
            )
        )
    return styled


def _expand_bbox(
    left: int,
    top: int,
    right: int,
    bottom: int,
    *,
    width: int,
    height: int,
    height_scale: float,
    width_scale: float = 1.0,
) -> tuple[int, int, int, int]:
    box_width = max(1, right - left)
    box_height = max(1, bottom - top)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    expanded_width = int(round(box_width * width_scale))
    expanded_height = int(round(box_height * height_scale))
    new_left = max(0, center_x - expanded_width // 2)
    new_right = min(width, center_x + expanded_width // 2)
    new_top = max(0, center_y - expanded_height // 2)
    new_bottom = min(height, center_y + expanded_height // 2)
    return new_left, new_top, new_right, new_bottom


def _filter_bands(
    bands: list[tuple[int, int, int, int, str, str | None]],
    image_size: tuple[int, int],
) -> list[tuple[int, int, int, int, str, str | None]]:
    min_height = max(8, int(image_size[1] * MIN_BAND_HEIGHT_RATIO))
    return [band for band in bands if band[3] - band[1] >= min_height]


def _expand_bands_to_count(
    bands: list[tuple[int, int, int, int, str, str | None]],
    count: int,
) -> list[tuple[int, int, int, int, str, str | None]]:
    if not bands or count <= 0:
        return []
    if len(bands) >= count:
        return bands[:count]
    top = min(band[1] for band in bands)
    bottom = max(band[3] for band in bands)
    left = min(band[0] for band in bands)
    right = max(band[2] for band in bands)
    slice_height = max(1, (bottom - top) // count)
    expanded: list[tuple[int, int, int, int, str, str | None]] = []
    for index in range(count):
        line_top = top + index * slice_height
        line_bottom = bottom if index == count - 1 else min(bottom, line_top + slice_height)
        nearest = min(bands, key=lambda band: abs(((band[1] + band[3]) // 2) - line_top))
        expanded.append((left, line_top, right, line_bottom, nearest[4], nearest[5]))
    return expanded


def _select_bands_for_caption(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
    caption_line_count: int | None,
) -> list[tuple[int, int, int, int, str, str | None]]:
    bands = _filter_bands(bands, template_size)
    if caption_line_count is None:
        return bands

    body_bands = [band for band in bands if band[5] is None]
    label_bands = [band for band in bands if band[5] is not None]
    if label_bands:
        body_count = max(0, caption_line_count - 1)
        selected_body = (
            body_bands[:body_count]
            if len(body_bands) >= body_count
            else _expand_bands_to_count(body_bands, body_count)
        )
        return selected_body + label_bands[-1:]
    if len(body_bands) >= caption_line_count:
        return body_bands[:caption_line_count]
    if body_bands:
        return _expand_bands_to_count(body_bands, caption_line_count)
    return []


def pdf_has_baked_placeholder_text(
    template: Image.Image,
    reference: Image.Image,
) -> bool:
    """Return True when the PDF raster includes baked text outside the reference."""
    aligned = _resize_reference(reference, template.size)
    width, height = template.size
    top_limit = int(height * 0.46)
    template_pixels = template.load()
    reference_pixels = aligned.load()

    template_score = 0
    reference_score = 0
    for y in range(top_limit):
        for x in range(0, width, 4):
            template_luma = sum(template_pixels[x, y][:3]) / 3
            reference_luma = sum(reference_pixels[x, y][:3]) / 3
            if template_luma >= TEXT_LUMINANCE_THRESHOLD:
                template_score += 1
            if reference_luma >= TEXT_LUMINANCE_THRESHOLD:
                reference_score += 1
    return template_score > reference_score + 40


def _layout_reference_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    body_bands = [band for band in bands if band[5] is None]
    label_bands = [band for band in bands if band[5] is not None]
    if not body_bands:
        return []

    width, height = template_size
    body_heights = [max(1, band[3] - band[1]) for band in body_bands]
    base_height = max(body_heights)
    body_font = max(24.0, base_height * 0.82 * REFERENCE_FONT_SCALE)
    line_box_height = max(1, int(round(body_font * 1.12)))
    line_gap = max(2, int(round(body_font * REFERENCE_BODY_LINE_GAP_FACTOR)))

    body_top = min(band[1] for band in body_bands)
    body_bottom = max(band[3] for band in body_bands)
    body_width = int(width * 0.88)
    left = max(0, (width - body_width) // 2)
    right = min(width, left + body_width)

    total_body_height = (
        len(body_bands) * line_box_height + max(0, len(body_bands) - 1) * line_gap
    )
    body_block_center = (body_top + body_bottom) // 2
    start_y = max(0, body_block_center - total_body_height // 2)

    styles: list[TnLineStyle] = []
    for index, band in enumerate(body_bands):
        top = start_y + index * (line_box_height + line_gap)
        bottom = min(height, top + line_box_height)
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(left, top, right, bottom),
                font_size_px=body_font,
                color_hex=band[4],
                layer_name="reference-thumbnail",
                alignment="center",
                faux_bold=False,
                max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            )
        )

    if not label_bands:
        return styles

    label_band = label_bands[-1]
    label_top = label_band[1]
    label_font = max(18.0, body_font * REFERENCE_LABEL_FONT_RATIO)
    label_gap = max(6, int(round(body_font * REFERENCE_LABEL_GAP_FACTOR)))
    label_box_top = min(height, start_y + total_body_height + label_gap)
    label_center_x = (label_band[0] + label_band[2]) // 2
    label_width = int(round(width * REFERENCE_LABEL_BOX_WIDTH_RATIO))
    label_left = max(0, label_center_x - label_width // 2)
    label_right = min(width, label_left + label_width)
    label_box_height = max(
        int(round(label_font * REFERENCE_LABEL_BOX_HEIGHT_FONT_FACTOR)),
        int(round(label_width * REFERENCE_LABEL_BOX_HEIGHT_WIDTH_RATIO)),
    )
    label_box_bottom = min(height, label_box_top + label_box_height)
    styles.append(
        TnLineStyle(
            placeholder_text="reference-label",
            rendered_text="reference-label",
            bbox=(label_left, label_box_top, label_right, label_box_bottom),
            font_size_px=label_font,
            fixed_font_size_px=label_font,
            color_hex=label_band[4],
            layer_name="reference-thumbnail",
            alignment="center",
            faux_bold=False,
            max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            stacked_line_backgrounds=(label_band[5],) if label_band[5] else (),
        )
    )
    return styles


def extract_line_styles_from_reference_thumbnail(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int | None = None,
) -> list[TnLineStyle]:
    """Infer TN text boxes, colors, and optional label backgrounds from a reference image."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _select_bands_for_caption(
        _text_bands(aligned),
        template_size,
        caption_line_count,
    )
    if not bands:
        return []

    return _layout_reference_line_styles(bands, template_size)


def reference_text_cover_bounds(
    reference: Image.Image,
    *,
    scan_start_ratio: float = SCAN_START_RATIO,
) -> tuple[int, int, int, int] | None:
    """Return a padded rectangle covering detected reference text bands."""
    aligned = reference.convert("RGB")
    bands = _filter_bands(
        _text_bands(aligned, scan_start_ratio=scan_start_ratio),
        aligned.size,
    )
    if not bands:
        return None

    width, height = aligned.size
    left = min(band[0] for band in bands)
    top = min(band[1] for band in bands)
    right = max(band[2] for band in bands)
    bottom = max(band[3] for band in bands)
    margin_x = max(12, int(width * 0.04))
    margin_y = max(12, int(height * 0.02))
    return (
        max(0, left - margin_x),
        max(0, top - margin_y),
        min(width, right + margin_x),
        min(height, bottom + margin_y),
    )


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


def cover_reference_text(
    image: Image.Image,
    *,
    scan_start_ratio: float = SCAN_START_RATIO,
    solid_fill: bool = False,
) -> Image.Image:
    """Blur over original English text detected on a reference thumbnail."""
    cover_bounds = reference_text_cover_bounds(image, scan_start_ratio=scan_start_ratio)
    if cover_bounds is None:
        return image.copy()
    return cover_text_region(image, cover_bounds, solid_fill=solid_fill)


def load_reference_thumbnail(path: Path) -> Image.Image | None:
    if not path.is_file():
        return None
    with Image.open(path) as image:
        return image.convert("RGB")
