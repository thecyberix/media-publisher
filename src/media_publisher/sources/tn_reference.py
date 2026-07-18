"""Derive TN line styles from an original-platform reference thumbnail."""
from __future__ import annotations

from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("TN reference helpers require Pillow") from exc

from dataclasses import replace

from media_publisher.sources.tn_psd import TnLineStyle, TnTextSegment
from media_publisher.sources.tn_renderer import TEXT_EDGE_MARGIN_PX

TEXT_LUMINANCE_THRESHOLD = 180
TEXT_ROW_PIXEL_RATIO = 0.02
SCAN_START_RATIO = 0.55
SPLIT_TOP_TEXT_MAX_RATIO = 0.28
SPLIT_BOTTOM_TEXT_MIN_RATIO = 0.57
TOP_ONLY_TEXT_MAX_RATIO = 0.40
LABEL_BOX_MIN_RATIO = 0.52
LABEL_BOX_PANEL_WHITE_LUMINANCE = 200
LABEL_BOX_PANEL_MAX_SATURATION = 45
LABEL_BOX_HORIZONTAL_PADDING_RATIO = 0.06
LABEL_BOX_VERTICAL_PADDING_RATIO = 0.10
LABEL_BOX_MAX_GROW_FACTOR = 1.0
LABEL_BOX_FONT_SCALE = 0.44
LABEL_BOX_COVER_MARGIN_Y_RATIO = 0.08
LABEL_BOX_COVER_MARGIN_X = 4
LABEL_BOX_COVER_ROW_THRESHOLD = 90
LABEL_BOX_COVER_SCAN_INSET_RATIO = 0.10
LABEL_BOX_COVER_ROW_CLUSTER_GAP = 20
LABEL_BOX_COVER_SHIFT_Y_RATIO = -0.30
LABEL_BOX_COVER_WIDTH = 460
LABEL_BOX_COVER_HEIGHT = 140
LABEL_BOX_COVER_TEXT_PADDING_X_RATIO = 0.04
LABEL_BOX_COVER_TEXT_PADDING_Y_RATIO = 0.10
DARK_TEXT_LUMINANCE_THRESHOLD = 90
DARK_INK_LUMINANCE_THRESHOLD = 55
LABEL_BOX_INK_ROW_PIXEL_RATIO = 0.10
LABEL_BOX_BAND_MERGE_GAP = 8
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
TOP_ONLY_YELLOW_ACCENT_FONT_SCALE = 1.45
TOP_ONLY_BODY_LINE_GAP_FACTOR = -0.30
MYSTIC_MUSINGS_FIRST_GAP_FACTOR = 0.55
MYSTIC_MUSINGS_LINE1_FONT_SCALE = 1.22
MYSTIC_MUSINGS_LINE2_FONT_SCALE = 1.28
MYSTIC_MUSINGS_LINE3_FONT_SCALE = 1.70
MYSTIC_MUSINGS_TITLE_LINE_FONT_SCALE = 1.52
MYSTIC_MUSINGS_TITLE_LINE_GAP_FACTOR = 0.03
ENLIGHTENMENT_BODY_FONT = 62.0
ENLIGHTENMENT_ECSTASY_WORD_FONT = 118.0
ENLIGHTENMENT_LINE1_RAISE_RATIO = 0.032
ENLIGHTENMENT_TOP_LINES_GAP_FACTOR = 0.01
ENLIGHTENMENT_TOP_COVER_TOP_MARGIN_RATIO = 0.040
ENLIGHTENMENT_TOP_COVER_BOTTOM_MARGIN_RATIO = 0.028
ENLIGHTENMENT_TOP_COVER_SHIFT_DOWN_RATIO = -0.012
ENLIGHTENMENT_TOP_COVER_WIDTH_RATIO = 0.94
ENLIGHTENMENT_SADHGURU_COLOR = "#E8C04A"
ENLIGHTENMENT_BOTTOM_COVER_MIN_RATIO = 0.70
ENLIGHTENMENT_BOTTOM_COVER_TOP_MARGIN_RATIO = 0.010
ENLIGHTENMENT_BOTTOM_COVER_BOTTOM_MARGIN_RATIO = 0.05
SPLIT_BOTTOM_COVER_TOP_MARGIN_RATIO = 0.004
SPLIT_BOTTOM_COVER_BOTTOM_MARGIN_RATIO = 0.04
RIGHT_SIDE_SCAN_LEFT_RATIO = 0.45
RIGHT_SIDE_SCAN_TOP_RATIO = 0.12
RIGHT_SIDE_SCAN_BOTTOM_RATIO = 0.50
RIGHT_SIDE_MIN_BANDS = 2
RIGHT_SIDE_TEXT_AVG_LUMINANCE = 160
RIGHT_SIDE_BOX_LEFT_RATIO = 0.42
RIGHT_SIDE_BOX_RIGHT_MARGIN_RATIO = 0.025
RIGHT_SIDE_FONT_SCALE = 0.72
RIGHT_SIDE_LINE1_FONT_SCALE = 0.80
RIGHT_SIDE_LINE3_FONT_SCALE = 1.18
RIGHT_SIDE_COVER_MARGIN_X_RATIO = 0.03
RIGHT_SIDE_COVER_HEIGHT = 135
RIGHT_SIDE_PANEL_OLIVE = (50, 59, 39)
RIGHT_SIDE_PANEL_OLIVE_ALPHA = 0.45
RIGHT_SIDE_PANEL_TRIM_PAD_RATIO = 0.004


def _is_text_pixel(red: int, green: int, blue: int) -> bool:
    luminance = (red + green + blue) / 3
    if luminance >= TEXT_LUMINANCE_THRESHOLD:
        return True
    if red > 160 and green > 120 and blue < red - 40:
        return True
    # Cyan/blue title text on dark backgrounds (e.g. Adiyogi top captions).
    return blue >= 100 and green >= 60 and blue > red + 20 and luminance >= 70


def _is_dark_ink_pixel(red: int, green: int, blue: int) -> bool:
    luminance = (red + green + blue) / 3
    if luminance >= DARK_INK_LUMINANCE_THRESHOLD:
        return False
    return max(red, green, blue) - min(red, green, blue) < 85


def _ink_row_clusters_in_band(
    reference: Image.Image,
    *,
    scan_left: int,
    scan_right: int,
    band_top: int,
    band_bottom: int,
    row_threshold: int,
) -> list[tuple[int, int]]:
    pixels = reference.load()
    active_rows: list[int] = []
    for y in range(band_top, band_bottom):
        ink_count = sum(
            1
            for x in range(scan_left, scan_right)
            if _is_dark_ink_pixel(*pixels[x, y][:3])
        )
        if ink_count >= row_threshold:
            active_rows.append(y)
    if not active_rows:
        return []

    clusters: list[tuple[int, int]] = []
    cluster_start = active_rows[0]
    previous = active_rows[0]
    for y in active_rows[1:]:
        if y == previous + 1:
            previous = y
            continue
        clusters.append((cluster_start, previous))
        cluster_start = y
        previous = y
    clusters.append((cluster_start, previous))
    return clusters


def _compute_label_box_cover_rect(
    reference: Image.Image,
    panel: tuple[int, int, int, int],
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    panel_left, panel_top, panel_right, panel_bottom = panel
    panel_width = max(1, panel_right - panel_left)
    panel_height = max(1, panel_bottom - panel_top)
    template_width, _template_height = template_size
    scan_left = panel_left + int(panel_width * LABEL_BOX_COVER_SCAN_INSET_RATIO)
    scan_right = panel_right - int(panel_width * LABEL_BOX_COVER_SCAN_INSET_RATIO)
    pixels = reference.load()
    shift_y = int(panel_height * LABEL_BOX_COVER_SHIFT_Y_RATIO)

    cluster_tops: list[int] = []
    cluster_bottoms: list[int] = []
    min_x = scan_right
    max_x = scan_left
    for _left, band_top, _right, band_bottom, *_rest in bands:
        clusters = _merge_row_clusters(
            _ink_row_clusters_in_band(
                reference,
                scan_left=scan_left,
                scan_right=scan_right,
                band_top=band_top,
                band_bottom=band_bottom,
                row_threshold=LABEL_BOX_COVER_ROW_THRESHOLD,
            ),
            merge_gap=LABEL_BOX_COVER_ROW_CLUSTER_GAP,
        )
        if not clusters:
            continue
        cluster_top, cluster_bottom = max(clusters, key=lambda item: item[1] - item[0])
        cluster_tops.append(cluster_top)
        cluster_bottoms.append(cluster_bottom)
        for y in range(cluster_top, cluster_bottom + 1):
            for x in range(scan_left, scan_right):
                if _is_dark_ink_pixel(*pixels[x, y][:3]):
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)

    if not cluster_tops:
        return None

    ink_top = min(cluster_tops)
    ink_bottom = max(cluster_bottoms)
    anchor_y = (ink_top + ink_bottom) // 2 + shift_y
    anchor_x = (min_x + max_x) // 2 if min_x < max_x else template_width // 2
    cover_top = max(panel_top, anchor_y - LABEL_BOX_COVER_HEIGHT // 2)
    cover_bottom = cover_top + LABEL_BOX_COVER_HEIGHT
    if cover_bottom > panel_bottom:
        cover_bottom = panel_bottom
        cover_top = max(panel_top, cover_bottom - LABEL_BOX_COVER_HEIGHT)

    cover_left = max(0, anchor_x - LABEL_BOX_COVER_WIDTH // 2)
    cover_right = cover_left + LABEL_BOX_COVER_WIDTH
    if cover_right > template_width:
        cover_right = template_width
        cover_left = max(0, cover_right - LABEL_BOX_COVER_WIDTH)
    cover_left = max(panel_left, cover_left)
    cover_right = min(panel_right, cover_left + LABEL_BOX_COVER_WIDTH)
    cover_left = max(panel_left, cover_right - LABEL_BOX_COVER_WIDTH)
    if cover_bottom <= cover_top:
        return None
    return (cover_left, cover_top, cover_right, cover_bottom)


def _label_box_cover_rects(
    reference: Image.Image,
    panel: tuple[int, int, int, int],
    bands: list[tuple[int, int, int, int, str, str | None]],
) -> list[tuple[int, int, int, int]]:
    cover_rect = _compute_label_box_cover_rect(
        reference,
        panel,
        bands,
        reference.size,
    )
    if cover_rect is None:
        return []
    return [cover_rect]


def _merge_row_clusters(
    clusters: list[tuple[int, int]],
    *,
    merge_gap: int,
) -> list[tuple[int, int]]:
    if not clusters:
        return []
    ordered = sorted(clusters, key=lambda item: item[0])
    merged: list[tuple[int, int]] = [ordered[0]]
    for cluster_top, cluster_bottom in ordered[1:]:
        previous_top, previous_bottom = merged[-1]
        if cluster_top - previous_bottom <= merge_gap:
            merged[-1] = (previous_top, cluster_bottom)
            continue
        merged.append((cluster_top, cluster_bottom))
    return merged


def _merge_nearby_bands(
    bands: list[tuple[int, int, int, int, str, str | None]],
    *,
    merge_gap: int,
) -> list[tuple[int, int, int, int, str, str | None]]:
    if not bands:
        return []
    ordered = sorted(bands, key=lambda band: band[1])
    merged: list[tuple[int, int, int, int, str, str | None]] = [ordered[0]]
    for band in ordered[1:]:
        previous = merged[-1]
        if band[1] - previous[3] <= merge_gap:
            merged[-1] = (
                min(previous[0], band[0]),
                previous[1],
                max(previous[2], band[2]),
                band[3],
                previous[4],
                previous[5],
            )
            continue
        merged.append(band)
    return merged


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


def _dark_text_bands_in_region(
    reference: Image.Image,
    *,
    top: int,
    bottom: int,
    region_left: int | None = None,
    region_right: int | None = None,
    ink_only: bool = False,
    merge_gap: int = 0,
) -> list[tuple[int, int, int, int, str, str | None]]:
    width, height = reference.size
    pixels = reference.load()
    y_start = max(0, top)
    y_end = min(height, bottom)
    scan_left = max(0, region_left if region_left is not None else 0)
    scan_right = min(width, region_right if region_right is not None else width)
    scan_width = max(1, scan_right - scan_left)
    if ink_only:
        row_threshold = max(8, int(scan_width * LABEL_BOX_INK_ROW_PIXEL_RATIO))
        is_dark = _is_dark_ink_pixel
    else:
        row_threshold = max(1, int(width * TEXT_ROW_PIXEL_RATIO))
        is_dark = lambda red, green, blue: (red + green + blue) / 3 < DARK_TEXT_LUMINANCE_THRESHOLD

    raw_bands: list[tuple[int, int, int, int]] = []
    in_band = False
    band_start = 0
    for y in range(y_start, y_end):
        dark_count = 0
        for x in range(scan_left, scan_right):
            red, green, blue = pixels[x, y][:3]
            if is_dark(red, green, blue):
                dark_count += 1
        if dark_count > row_threshold and not in_band:
            in_band = True
            band_start = y
        elif dark_count <= row_threshold and in_band:
            in_band = False
            raw_bands.append((0, band_start, width, y))
    if in_band:
        raw_bands.append((0, band_start, width, y_end))

    styled: list[tuple[int, int, int, int, str, str | None]] = []
    for _left, band_top, _right, band_bottom in raw_bands:
        text_pixels: list[tuple[int, int, int]] = []
        min_x = width
        max_x = 0
        for y in range(band_top, band_bottom):
            for x in range(scan_left, scan_right):
                red, green, blue = pixels[x, y][:3]
                if is_dark(red, green, blue):
                    text_pixels.append((red, green, blue))
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
        if min_x >= max_x:
            continue
        margin_x = max(8, int(width * 0.04))
        left = max(0, min_x - margin_x)
        right = min(width, max_x + margin_x)
        styled.append(
            (
                left,
                band_top,
                right,
                band_bottom,
                _hex_from_rgb(*_average_rgb(text_pixels)),
                None,
            )
        )
    if merge_gap > 0:
        return _merge_nearby_bands(styled, merge_gap=merge_gap)
    return styled


def _is_panel_white_pixel(red: int, green: int, blue: int) -> bool:
    luminance = (red + green + blue) / 3
    saturation = max(red, green, blue) - min(red, green, blue)
    return luminance >= LABEL_BOX_PANEL_WHITE_LUMINANCE and saturation <= LABEL_BOX_PANEL_MAX_SATURATION


def _light_panel_bounds(reference: Image.Image) -> tuple[int, int, int, int] | None:
    width, height = reference.size
    pixels = reference.load()
    y_start = int(height * LABEL_BOX_MIN_RATIO)
    row_threshold = max(1, int(width * 0.55))
    bright_rows: list[int] = []
    for y in range(y_start, height):
        bright_count = sum(
            1 for x in range(width) if _is_panel_white_pixel(*pixels[x, y][:3])
        )
        if bright_count >= row_threshold:
            bright_rows.append(y)
    if len(bright_rows) < 12:
        return None

    top = bright_rows[0]
    bottom = bright_rows[-1]
    left = width
    right = 0
    sample_rows = bright_rows[:: max(1, len(bright_rows) // 8)]
    for y in sample_rows:
        for x in range(width):
            if _is_panel_white_pixel(*pixels[x, y][:3]):
                left = min(left, x)
                right = max(right, x)
    if right <= left:
        return None
    margin_x = max(8, int(width * 0.02))
    margin_y = max(8, int(height * 0.01))
    return (
        max(0, left - margin_x),
        max(0, top - margin_y),
        min(width, right + margin_x),
        min(height, bottom + margin_y),
    )


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


def _split_top_bottom_bands(
    bands: list[tuple[int, int, int, int, str, str | None]],
    height: int,
) -> tuple[
    list[tuple[int, int, int, int, str, str | None]],
    list[tuple[int, int, int, int, str, str | None]],
]:
    top_limit = int(height * SPLIT_TOP_TEXT_MAX_RATIO)
    bottom_start = int(height * SPLIT_BOTTOM_TEXT_MIN_RATIO)
    top = [band for band in bands if band[3] <= top_limit]
    bottom = [band for band in bands if band[1] >= bottom_start]
    return top, bottom


def _cover_band_group(
    image: Image.Image,
    bands: list[tuple[int, int, int, int, str, str | None]],
    *,
    solid_fill: bool,
    margin_y_top: int | None = None,
    margin_y_bottom: int | None = None,
    cover_width_ratio: float | None = None,
    shift_y: int = 0,
) -> Image.Image:
    if not bands:
        return image

    width, height = image.size
    band_left = min(band[0] for band in bands)
    top = min(band[1] for band in bands)
    band_right = max(band[2] for band in bands)
    bottom = max(band[3] for band in bands)
    margin_x = max(12, int(width * 0.04))
    default_margin_y = max(12, int(height * 0.02))
    top_margin = default_margin_y if margin_y_top is None else margin_y_top
    bottom_margin = default_margin_y if margin_y_bottom is None else margin_y_bottom
    if cover_width_ratio is not None:
        cover_width = max(1, int(round(width * cover_width_ratio)))
        left = max(0, (width - cover_width) // 2)
        right = min(width, left + cover_width)
    else:
        left = max(0, band_left - margin_x)
        right = min(width, band_right + margin_x)
    bounds = (
        left,
        max(0, top - top_margin + shift_y),
        right,
        min(height, bottom + bottom_margin + shift_y),
    )
    return cover_text_region(image, bounds, solid_fill=solid_fill)


def has_top_only_reference_layout(
    reference: Image.Image,
    template_size: tuple[int, int],
) -> bool:
    """Return True when English text sits in the upper region only."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), template_size)
    height = template_size[1]
    top_limit = int(height * TOP_ONLY_TEXT_MAX_RATIO)
    bottom_start = int(height * SPLIT_BOTTOM_TEXT_MIN_RATIO)
    top_bands = [band for band in bands if band[3] <= top_limit]
    bottom_bands = [band for band in bands if band[1] >= bottom_start]
    return len(top_bands) >= 1 and not bottom_bands


def extract_top_only_reference_line_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int,
) -> list[TnLineStyle]:
    """Derive line styles from top English text only."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), template_size)
    top_limit = int(template_size[1] * TOP_ONLY_TEXT_MAX_RATIO)
    top_bands = [
        (left, top, right, bottom, color, None)
        for left, top, right, bottom, color, _background in bands
        if bottom <= top_limit
    ]
    selected = _select_bands_for_caption(top_bands, template_size, caption_line_count)
    if not selected:
        return []
    return _layout_top_only_line_styles(selected, template_size, reference=aligned)


def cover_top_only_reference_text(
    image: Image.Image,
    *,
    solid_fill: bool = True,
    caption_line_count: int | None = None,
) -> Image.Image:
    """Cover English text detected in the upper region."""
    aligned = image.convert("RGB")
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), aligned.size)
    top_limit = int(aligned.size[1] * TOP_ONLY_TEXT_MAX_RATIO)
    top = [band for band in bands if band[3] <= top_limit]
    selected = _select_bands_for_caption(top, aligned.size, caption_line_count)
    if not selected:
        selected = top[:2]
    margin_y = max(10, int(aligned.size[1] * 0.012))
    return _cover_band_group(
        image,
        selected,
        solid_fill=solid_fill,
        margin_y_top=margin_y,
        margin_y_bottom=margin_y,
        cover_width_ratio=1.0,
    )


def _right_side_text_bands(
    reference: Image.Image,
) -> list[tuple[int, int, int, int, str, str | None]]:
    """Detect bright caption lines constrained to the right column."""
    width, height = reference.size
    pixels = reference.load()
    scan_left = int(width * RIGHT_SIDE_SCAN_LEFT_RATIO)
    scan_right = width
    scan_top = int(height * RIGHT_SIDE_SCAN_TOP_RATIO)
    scan_bottom = int(height * RIGHT_SIDE_SCAN_BOTTOM_RATIO)
    scan_width = max(1, scan_right - scan_left)
    row_threshold = max(1, int(scan_width * TEXT_ROW_PIXEL_RATIO))

    raw_bands: list[tuple[int, int]] = []
    in_band = False
    band_start = 0
    for y in range(scan_top, scan_bottom):
        bright_count = 0
        for x in range(scan_left, scan_right):
            red, green, blue = pixels[x, y][:3]
            if _is_text_pixel(red, green, blue):
                bright_count += 1
        if bright_count > row_threshold and not in_band:
            in_band = True
            band_start = y
        elif bright_count <= row_threshold and in_band:
            in_band = False
            raw_bands.append((band_start, y))
    if in_band:
        raw_bands.append((band_start, scan_bottom))

    styled: list[tuple[int, int, int, int, str, str | None]] = []
    for top, bottom in raw_bands:
        text_pixels: list[tuple[int, int, int]] = []
        min_x = width
        max_x = 0
        for y in range(top, bottom):
            for x in range(scan_left, scan_right):
                red, green, blue = pixels[x, y][:3]
                if _is_text_pixel(red, green, blue):
                    text_pixels.append((red, green, blue))
                    min_x = min(min_x, x)
                    max_x = max(max_x, x)
        if min_x >= max_x or not text_pixels:
            continue
        text_color = _average_rgb(text_pixels)
        if sum(text_color) / 3 < RIGHT_SIDE_TEXT_AVG_LUMINANCE:
            continue
        margin_x = max(6, int(width * 0.02))
        styled.append(
            (
                max(0, min_x - margin_x),
                top,
                min(width, max_x + margin_x),
                bottom,
                _hex_from_rgb(*text_color),
                None,
            )
        )
    return styled


def has_right_side_reference_layout(
    reference: Image.Image,
    template_size: tuple[int, int],
) -> bool:
    """Return True when English caption sits in a right-side overlay column."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_right_side_text_bands(aligned), template_size)
    if len(bands) < RIGHT_SIDE_MIN_BANDS:
        return False
    width = template_size[0]
    # Require the detected lines to stay in the right half overall.
    median_left = sorted(band[0] for band in bands)[len(bands) // 2]
    return median_left >= int(width * 0.40)


def _layout_right_side_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    if not bands:
        return []

    width, height = template_size
    box_left = int(width * RIGHT_SIDE_BOX_LEFT_RATIO)
    box_right = width - max(8, int(width * RIGHT_SIDE_BOX_RIGHT_MARGIN_RATIO))

    styles: list[TnLineStyle] = []
    body_height = max(1, bands[min(1, len(bands) - 1)][3] - bands[min(1, len(bands) - 1)][1])
    body_font = max(22.0, body_height * RIGHT_SIDE_FONT_SCALE * REFERENCE_FONT_SCALE)
    for index, band in enumerate(bands):
        band_height = max(1, band[3] - band[1])
        if index == 0:
            font_size = max(22.0, band_height * RIGHT_SIDE_LINE1_FONT_SCALE * REFERENCE_FONT_SCALE)
        elif index == len(bands) - 1 and len(bands) >= 3:
            font_size = max(22.0, body_font * RIGHT_SIDE_LINE3_FONT_SCALE)
        else:
            font_size = body_font
        line_box_height = max(1, int(round(font_size * 1.12)))
        center_y = (band[1] + band[3]) // 2
        top = max(0, center_y - line_box_height // 2)
        bottom = min(height, top + line_box_height)
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(box_left, top, box_right, bottom),
                font_size_px=font_size,
                color_hex=band[4],
                layer_name="reference-thumbnail",
                alignment="right",
                faux_bold=False,
                max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            )
        )
    return styles


def extract_right_side_reference_line_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int,
) -> list[TnLineStyle]:
    """Derive right-aligned line styles from a right-column English caption."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_right_side_text_bands(aligned), template_size)
    selected = _select_bands_for_caption(bands, template_size, caption_line_count)
    if not selected:
        return []
    return _layout_right_side_line_styles(selected, template_size)


def _lift_right_side_olive_pixel(red: int, green: int, blue: int) -> tuple[int, int, int]:
    """Approximate removal of the right-column olive veil to reveal underlying photo."""
    alpha = RIGHT_SIDE_PANEL_OLIVE_ALPHA
    olive_r, olive_g, olive_b = RIGHT_SIDE_PANEL_OLIVE
    return (
        max(0, min(255, int(round((red - alpha * olive_r) / (1.0 - alpha))))),
        max(0, min(255, int(round((green - alpha * olive_g) / (1.0 - alpha))))),
        max(0, min(255, int(round((blue - alpha * olive_b) / (1.0 - alpha))))),
    )


def _trim_right_side_panel_outside_text(
    image: Image.Image,
    *,
    panel_left: int,
    panel_right: int,
    keep_top: int,
    keep_bottom: int,
) -> Image.Image:
    """Shorten the olive text background within the text column only.

    Restricted to the caption column so Sadhguru / composite edges are untouched.
    """
    result = image.copy()
    pixels = result.load()
    width, height = result.size
    left = max(0, panel_left)
    right = min(width, panel_right)
    if right <= left:
        return result
    for y in range(height):
        if keep_top <= y <= keep_bottom:
            continue
        for x in range(left, right):
            red, green, blue = pixels[x, y][:3]
            if _is_text_pixel(red, green, blue):
                continue
            luminance = (red + green + blue) / 3
            if luminance > 160:
                continue
            # Olive panel is green-dominant and fairly dark.
            if green < red - 5 or green < blue - 5:
                continue
            if luminance > 120 and (green - red) < 8 and (green - blue) < 8:
                continue
            pixels[x, y] = _lift_right_side_olive_pixel(red, green, blue)
    return result


def cover_right_side_reference_text(
    image: Image.Image,
    *,
    solid_fill: bool = True,
    caption_line_count: int | None = None,
) -> Image.Image:
    """Cover English text in the right-side caption column."""
    aligned = image.convert("RGB")
    bands = _filter_bands(_right_side_text_bands(aligned), aligned.size)
    selected = _select_bands_for_caption(bands, aligned.size, caption_line_count)
    if not selected:
        selected = bands
    if not selected:
        return image.copy()

    width, height = aligned.size
    # Restore previous horizontal cover from detected text bands.
    left = min(band[0] for band in selected)
    right = max(band[2] for band in selected)
    text_top = min(band[1] for band in selected)
    text_bottom = max(band[3] for band in selected)
    margin_x = max(10, int(width * RIGHT_SIDE_COVER_MARGIN_X_RATIO))
    cover_left = max(0, left - margin_x)
    cover_right = min(width, right + margin_x)
    # Fixed-height panel centered on the caption block.
    text_center = (text_top + text_bottom) // 2
    cover_height = min(RIGHT_SIDE_COVER_HEIGHT, height)
    cover_top = max(0, text_center - cover_height // 2)
    cover_bottom = cover_top + cover_height
    if cover_bottom > height:
        cover_bottom = height
        cover_top = max(0, cover_bottom - cover_height)

    # Paint a clean fixed-height panel only — do not alter pixels outside it
    # (olive-lift trimming caused edge artifacts on Sadhguru / the inset).
    result = image.copy()

    if not solid_fill:
        return cover_text_region(
            result,
            (cover_left, cover_top, cover_right, cover_bottom),
            solid_fill=False,
        )

    pixels = aligned.load()
    sample_pixels: list[tuple[int, int, int]] = []
    for y in range(cover_top, cover_bottom):
        for x in range(cover_left, cover_right):
            red, green, blue = pixels[x, y][:3]
            if _is_text_pixel(red, green, blue):
                continue
            if (red + green + blue) / 3 > 140:
                continue
            sample_pixels.append((red, green, blue))
    fill = _average_rgb(sample_pixels) if sample_pixels else (72, 83, 63)
    draw = ImageDraw.Draw(result)
    draw.rectangle((cover_left, cover_top, cover_right, cover_bottom), fill=fill)
    return result


def has_label_box_reference_layout(
    reference: Image.Image,
    template_size: tuple[int, int],
) -> bool:
    """Return True when English text is dark type inside a bright bottom panel."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    panel = _light_panel_bounds(aligned)
    if panel is None:
        return False
    left, top, right, bottom = panel
    dark_bands = _filter_bands(
        _dark_text_bands_in_region(
            aligned,
            top=top,
            bottom=bottom,
            region_left=left,
            region_right=right,
            ink_only=True,
            merge_gap=LABEL_BOX_BAND_MERGE_GAP,
        ),
        template_size,
    )
    return len(dark_bands) >= 1


def _label_box_text_bands(
    reference: Image.Image,
    panel: tuple[int, int, int, int],
) -> list[tuple[int, int, int, int, str, str | None]]:
    left, top, right, bottom = panel
    bands = _dark_text_bands_in_region(
        reference,
        top=top,
        bottom=bottom,
        region_left=left,
        region_right=right,
        ink_only=True,
        merge_gap=LABEL_BOX_BAND_MERGE_GAP,
    )
    return bands


def _layout_label_box_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
    *,
    panel: tuple[int, int, int, int],
    cover_rect: tuple[int, int, int, int] | None = None,
) -> list[TnLineStyle]:
    panel_left, panel_top, panel_right, panel_bottom = panel
    if cover_rect is not None:
        cover_left, cover_top, cover_right, cover_bottom = cover_rect
        cover_width = max(1, cover_right - cover_left)
        cover_height = max(1, cover_bottom - cover_top)
        padding_x = max(8, int(round(cover_width * LABEL_BOX_COVER_TEXT_PADDING_X_RATIO)))
        padding_y = max(6, int(round(cover_height * LABEL_BOX_COVER_TEXT_PADDING_Y_RATIO)))
        inner_left = cover_left + padding_x
        inner_right = cover_right - padding_x
        inner_height = max(1, cover_height - 2 * padding_y)
    else:
        panel_width = max(1, panel_right - panel_left)
        panel_height = max(1, panel_bottom - panel_top)
        padding_x = max(8, int(round(panel_width * LABEL_BOX_HORIZONTAL_PADDING_RATIO)))
        padding_y = max(8, int(round(panel_height * LABEL_BOX_VERTICAL_PADDING_RATIO)))
        inner_left = panel_left + padding_x
        inner_right = panel_right - padding_x
        inner_height = max(1, panel_bottom - panel_top - 2 * padding_y)
        cover_top = panel_top + padding_y

    band_heights = [max(1, band[3] - band[1]) for band in bands]
    reference_font = max(
        14.0,
        sum(band_heights) / len(band_heights) * LABEL_BOX_FONT_SCALE * REFERENCE_FONT_SCALE,
    )
    line_count = max(1, len(bands))
    line_gap = max(4, int(round(reference_font * REFERENCE_BODY_LINE_GAP_FACTOR)))
    max_font_for_height = inner_height / (line_count * 1.18 + max(0, line_count - 1) * 0.08)
    font_size = min(reference_font, max_font_for_height)
    line_height = max(1, int(round(font_size * 1.12)))
    total_text_height = line_count * line_height + max(0, line_count - 1) * line_gap
    if cover_rect is not None:
        start_y = cover_top + padding_y + max(0, (inner_height - total_text_height) // 2)
    else:
        inner_top = panel_top + padding_y
        start_y = inner_top + max(0, (inner_height - total_text_height) // 2)
        start_y = max(0, start_y)

    styles: list[TnLineStyle] = []
    for index, band in enumerate(bands):
        top = start_y + index * (line_height + line_gap)
        bottom = min(template_size[1], top + line_height)
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(inner_left, top, inner_right, bottom),
                font_size_px=font_size,
                color_hex=band[4],
                layer_name="reference-thumbnail",
                alignment="center",
                faux_bold=False,
                max_grow_factor=LABEL_BOX_MAX_GROW_FACTOR,
            )
        )
    return styles


def _select_label_box_bands(
    bands: list[tuple[int, int, int, int, str, str | None]],
    caption_line_count: int,
) -> list[tuple[int, int, int, int, str, str | None]]:
    if not bands or caption_line_count <= 0:
        return []
    merged = _merge_nearby_bands(sorted(bands, key=lambda band: band[1]), merge_gap=40)
    if len(merged) >= caption_line_count:
        return sorted(merged[:caption_line_count], key=lambda band: band[1])
    min_height = 30
    prominent = [band for band in merged if band[3] - band[1] >= min_height]
    if len(prominent) >= caption_line_count:
        selected = sorted(
            prominent,
            key=lambda band: band[3] - band[1],
            reverse=True,
        )[:caption_line_count]
        return sorted(selected, key=lambda band: band[1])
    if merged:
        return _expand_bands_to_count(merged, caption_line_count)
    return []


def extract_label_box_reference_line_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int,
) -> list[TnLineStyle]:
    """Derive line styles from dark text inside a bright bottom label box."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    panel = _light_panel_bounds(aligned)
    if panel is None:
        return []
    dark_bands = _filter_bands(_label_box_text_bands(aligned, panel), template_size)
    selected = _select_label_box_bands(dark_bands, caption_line_count)
    if not selected:
        return []
    cover_rect = _compute_label_box_cover_rect(aligned, panel, selected, template_size)
    return _layout_label_box_line_styles(
        selected,
        template_size,
        panel=panel,
        cover_rect=cover_rect,
    )


def cover_label_box_reference_text(
    image: Image.Image,
    *,
    solid_fill: bool = True,
) -> Image.Image:
    """Cover dark English text inside the bright bottom label box."""
    aligned = image.convert("RGB")
    panel = _light_panel_bounds(aligned)
    if panel is None:
        return image.copy()
    left, top, right, bottom = panel
    result = image.copy()
    panel_pixels = aligned.load()
    sample = _average_rgb(
        [
            panel_pixels[x, y][:3]
            for y in range(top, min(top + 12, bottom))
            for x in range(left, min(left + 24, right))
            if _is_panel_white_pixel(*panel_pixels[x, y][:3])
        ]
    )
    dark_bands = _filter_bands(_label_box_text_bands(aligned, panel), aligned.size)
    prominent = _select_label_box_bands(dark_bands, 2)
    draw = ImageDraw.Draw(result)
    for cover_left, cover_top, cover_right, cover_bottom in _label_box_cover_rects(
        aligned,
        panel,
        prominent,
    ):
        draw.rectangle(
            (cover_left, cover_top, cover_right, cover_bottom),
            fill=sample,
        )
    if prominent:
        return result
    for band_left, band_top, band_right, band_bottom, *_rest in dark_bands:
        margin_x = max(4, int((band_right - band_left) * 0.02))
        margin_y = max(2, int((band_bottom - band_top) * 0.15))
        draw.rectangle(
            (
                max(left, band_left - margin_x),
                max(top, band_top - margin_y),
                min(right, band_right + margin_x),
                min(bottom, band_bottom + margin_y),
            ),
            fill=sample,
        )
    return result


def has_split_top_bottom_reference_layout(
    reference: Image.Image,
    template_size: tuple[int, int],
) -> bool:
    """Return True when English text appears in both top and bottom regions."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), template_size)
    top, bottom = _split_top_bottom_bands(bands, template_size[1])
    return len(top) >= 2 and len(bottom) >= 1


def _is_enlightenment_top_layout(
    bands: list[tuple[int, int, int, int, str, str | None]],
) -> bool:
    if len(bands) != 4:
        return False
    heights = [max(1, band[3] - band[1]) for band in bands]
    return heights[1] >= max(heights[0], heights[2], heights[3]) * 1.5


def _line_box_height(font_size: float) -> int:
    return max(1, int(round(font_size * 1.14)))


def _enlightenment_top_cover_margins(height: int) -> tuple[int, int, int]:
    top_margin = max(28, int(height * ENLIGHTENMENT_TOP_COVER_TOP_MARGIN_RATIO))
    bottom_margin = max(24, int(height * ENLIGHTENMENT_TOP_COVER_BOTTOM_MARGIN_RATIO))
    shift_y = int(height * ENLIGHTENMENT_TOP_COVER_SHIFT_DOWN_RATIO)
    return top_margin, bottom_margin, shift_y


def _enlightenment_top_cover_vertical_bounds(
    top_bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> tuple[int, int]:
    height = template_size[1]
    top = min(band[1] for band in top_bands)
    bottom = max(band[3] for band in top_bands)
    top_margin, bottom_margin, shift_y = _enlightenment_top_cover_margins(height)
    cover_top = max(0, top - top_margin + shift_y)
    cover_bottom = min(height, bottom + bottom_margin + shift_y)
    return cover_top, cover_bottom


def _center_enlightenment_top_line_centers(
    cover_top: int,
    cover_bottom: int,
    *,
    body_font: float,
    ecstasy_font: float,
) -> tuple[int, int]:
    line1_height = _line_box_height(body_font)
    line2_height = _line_box_height(ecstasy_font)
    gap = max(0, int(round(body_font * ENLIGHTENMENT_TOP_LINES_GAP_FACTOR)))
    block_height = line1_height + gap + line2_height
    cover_height = max(1, cover_bottom - cover_top)
    block_top = cover_top + max(0, (cover_height - block_height) // 2)
    line1_y = block_top + line1_height // 2
    line2_y = block_top + line1_height + gap + line2_height // 2
    return line1_y, line2_y


def _line_bbox_for_font(
    center_y: int,
    font_size: float,
    template_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    width, height = template_size
    line_box_height = _line_box_height(font_size)
    max_body_width = max(1, width - 2 * TEXT_EDGE_MARGIN_PX)
    body_width = min(int(width * 0.88), max_body_width)
    left = max(TEXT_EDGE_MARGIN_PX, (width - body_width) // 2)
    right = min(width - TEXT_EDGE_MARGIN_PX, left + body_width)
    top = max(0, center_y - line_box_height // 2)
    bottom = min(height, top + line_box_height)
    return left, top, right, bottom


def _band_center_y(band: tuple[int, int, int, int, str, str | None]) -> int:
    return (band[1] + band[3]) // 2


def _is_blue_text_pixel(red: int, green: int, blue: int) -> bool:
    luminance = (red + green + blue) / 3
    return blue > red + 20 and green >= 60 and 70 <= luminance < TEXT_LUMINANCE_THRESHOLD


def _is_white_text_pixel(red: int, green: int, blue: int) -> bool:
    return (red + green + blue) / 3 >= TEXT_LUMINANCE_THRESHOLD


def _is_yellow_text_pixel(red: int, green: int, blue: int) -> bool:
    return (
        red >= 140
        and green >= 140
        and blue <= 130
        and (red + green) / 2 > blue + 40
        and abs(red - green) < 80
    )


def _two_tone_segments_from_band(
    reference: Image.Image,
    band: tuple[int, int, int, int, str, str | None],
    *,
    body_font: float,
) -> tuple[TnTextSegment, ...] | None:
    left, top, right, bottom, _color, _background = band
    if right <= left or bottom <= top:
        return None

    pixels = reference.load()
    width = right - left
    blue_columns: list[int] = []
    white_columns: list[int] = []
    blue_pixels: list[tuple[int, int, int]] = []
    white_pixels: list[tuple[int, int, int]] = []

    for index in range(width):
        blue_count = 0
        white_count = 0
        for y in range(top, bottom):
            red, green, blue = pixels[left + index, y][:3]
            if _is_white_text_pixel(red, green, blue):
                white_count += 1
                white_pixels.append((red, green, blue))
            elif _is_blue_text_pixel(red, green, blue):
                blue_count += 1
                blue_pixels.append((red, green, blue))
        if blue_count > white_count and blue_count >= 5:
            blue_columns.append(index)
        elif white_count > blue_count and white_count >= 5:
            white_columns.append(index)

    if not blue_columns or not white_columns:
        return None
    if min(white_columns) <= max(blue_columns):
        return None

    blue_hex = _hex_from_rgb(*_average_rgb(blue_pixels))
    white_hex = _hex_from_rgb(*_average_rgb(white_pixels))
    return (
        TnTextSegment("WHEN ", body_font, blue_hex),
        TnTextSegment("ADIYOGI", body_font, white_hex),
    )


def _white_yellow_segments_from_band(
    reference: Image.Image,
    band: tuple[int, int, int, int, str, str | None],
    *,
    body_font: float,
) -> tuple[TnTextSegment, ...] | None:
    """Detect white body text followed by a yellow accent word (e.g. Sadhguru?)."""
    left, top, right, bottom, _color, _background = band
    if right <= left or bottom <= top:
        return None

    pixels = reference.load()
    white_pixels: list[tuple[int, int, int]] = []
    yellow_pixels: list[tuple[int, int, int]] = []
    column_stats: list[tuple[int, int, int]] = []
    for x in range(left, right):
        white_count = 0
        yellow_count = 0
        for y in range(top, bottom):
            red, green, blue = pixels[x, y][:3]
            if _is_white_text_pixel(red, green, blue):
                white_count += 1
                white_pixels.append((red, green, blue))
            elif _is_yellow_text_pixel(red, green, blue):
                yellow_count += 1
                yellow_pixels.append((red, green, blue))
        column_stats.append((x, white_count, yellow_count))

    ink = [x for x, white_count, yellow_count in column_stats if white_count + yellow_count >= 5]
    if len(ink) < 40 or not white_pixels or not yellow_pixels:
        return None

    ink_left = min(ink)
    ink_right = max(ink)
    best_split: tuple[float, int] | None = None
    for split in range(ink_left + 40, ink_right - 40):
        left_white = 0
        left_yellow = 0
        right_white = 0
        right_yellow = 0
        for x, white_count, yellow_count in column_stats:
            if ink_left <= x < split:
                left_white += white_count
                left_yellow += yellow_count
            elif split <= x <= ink_right:
                right_white += white_count
                right_yellow += yellow_count
        left_total = left_white + left_yellow
        right_total = right_white + right_yellow
        if left_total == 0 or right_total == 0:
            continue
        left_white_ratio = left_white / left_total
        right_yellow_ratio = right_yellow / right_total
        if left_white_ratio < 0.7 or right_yellow_ratio < 0.7:
            continue
        score = left_white_ratio + right_yellow_ratio
        if best_split is None or score > best_split[0]:
            best_split = (score, split)
    if best_split is None:
        return None

    split_x = best_split[1]
    left_white_pixels = [
        pixels[x, y][:3]
        for x in range(ink_left, split_x)
        for y in range(top, bottom)
        if _is_white_text_pixel(*pixels[x, y][:3])
    ]
    right_yellow_pixels = [
        pixels[x, y][:3]
        for x in range(split_x, ink_right + 1)
        for y in range(top, bottom)
        if _is_yellow_text_pixel(*pixels[x, y][:3])
    ]
    if not left_white_pixels or not right_yellow_pixels:
        return None

    white_hex = _hex_from_rgb(*_average_rgb(left_white_pixels))
    yellow_hex = _hex_from_rgb(*_average_rgb(right_yellow_pixels))
    accent_font = body_font * TOP_ONLY_YELLOW_ACCENT_FONT_SCALE
    return (
        TnTextSegment("Drawn to ", body_font, white_hex),
        TnTextSegment("Sadhguru?", accent_font, yellow_hex),
    )


def _layout_top_only_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
    *,
    reference: Image.Image,
) -> list[TnLineStyle]:
    if not bands:
        return []

    width, height = template_size
    max_body_width = max(1, width - 2 * TEXT_EDGE_MARGIN_PX)
    body_width = min(int(width * 0.88), max_body_width)
    box_left = max(TEXT_EDGE_MARGIN_PX, (width - body_width) // 2)
    box_right = min(width - TEXT_EDGE_MARGIN_PX, box_left + body_width)
    body_heights = [max(1, band[3] - band[1]) for band in bands]
    base_height = max(body_heights)
    body_font = max(24.0, base_height * 0.82 * REFERENCE_FONT_SCALE)

    line_segments: list[tuple[TnTextSegment, ...]] = []
    for index, band in enumerate(bands):
        segments: tuple[TnTextSegment, ...] = ()
        if index == 0:
            segments = _two_tone_segments_from_band(
                reference,
                band,
                body_font=body_font,
            ) or ()
        if not segments:
            segments = _white_yellow_segments_from_band(
                reference,
                band,
                body_font=body_font,
            ) or ()
        line_segments.append(segments)

    line_heights: list[int] = []
    for segments in line_segments:
        # Size boxes from body font so post-fit text doesn't sit in oversized padding.
        # Accent lines get a modest extra height for the larger yellow word.
        has_accent = bool(
            segments and max(segment.font_size_px for segment in segments) > body_font + 0.5
        )
        line_font = body_font * (1.15 if has_accent else 1.0)
        line_heights.append(max(1, int(round(line_font * 0.95))))
    line_gap = int(round(body_font * TOP_ONLY_BODY_LINE_GAP_FACTOR))

    body_top = min(band[1] for band in bands)
    body_bottom = max(band[3] for band in bands)
    total_body_height = sum(line_heights) + max(0, len(bands) - 1) * line_gap
    start_y = max(0, (body_top + body_bottom) // 2 - total_body_height // 2)

    styles: list[TnLineStyle] = []
    cursor_y = start_y
    for index, (band, segments, line_box_height) in enumerate(
        zip(bands, line_segments, line_heights, strict=True)
    ):
        top = cursor_y
        bottom = min(height, top + line_box_height)
        cursor_y = bottom + line_gap
        color_hex = band[4]
        if segments:
            color_hex = segments[0].color_hex
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(box_left, top, box_right, bottom),
                font_size_px=body_font,
                color_hex=color_hex,
                layer_name="reference-thumbnail",
                alignment="center",
                faux_bold=False,
                max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
                segments=segments,
                # Keep earlier lines the same rendered size as the last (usually longest) line.
                matched_font_size_style_index=(len(bands) - 1) if index < len(bands) - 1 else None,
            )
        )
    return styles


def _layout_enlightenment_line_styles(
    top_bands: list[tuple[int, int, int, int, str, str | None]],
    bottom_bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    if len(top_bands) < 2 or len(bottom_bands) < 2:
        return []

    body_font = ENLIGHTENMENT_BODY_FONT
    ecstasy_font = ENLIGHTENMENT_ECSTASY_WORD_FONT
    bottom_sorted = sorted(bottom_bands, key=lambda band: band[1])[-2:]
    cover_top, cover_bottom = _enlightenment_top_cover_vertical_bounds(top_bands, template_size)
    line1_y, line2_y = _center_enlightenment_top_line_centers(
        cover_top,
        cover_bottom,
        body_font=body_font,
        ecstasy_font=ecstasy_font,
    )
    line_specs: list[tuple[int, float, str, tuple[TnTextSegment, ...] | None]] = [
        (line1_y, body_font, top_bands[0][4], None),
        (
            line2_y,
            ecstasy_font,
            top_bands[1][4],
            (
                TnTextSegment("Екстаза ", ecstasy_font, top_bands[1][4]),
                TnTextSegment("на", body_font, top_bands[1][4]),
            ),
        ),
        (_band_center_y(bottom_sorted[0]), body_font, bottom_sorted[0][4], None),
        (
            _band_center_y(bottom_sorted[1]),
            body_font,
            "#FFFFFF",
            (
                TnTextSegment("със ", body_font, "#FFFFFF", faux_bold=False),
                TnTextSegment(
                    "Садгуру",
                    body_font,
                    ENLIGHTENMENT_SADHGURU_COLOR,
                    faux_bold=True,
                ),
            ),
        ),
    ]

    styles: list[TnLineStyle] = []
    for center_y, font_size, color_hex, segments in line_specs:
        box_font = font_size
        if segments:
            box_font = max(segment.font_size_px for segment in segments)
        segment_sizes = {segment.font_size_px for segment in segments} if segments else set()
        style = TnLineStyle(
            placeholder_text="reference-line",
            rendered_text="reference-line",
            bbox=_line_bbox_for_font(center_y, box_font, template_size),
            font_size_px=font_size,
            fixed_font_size_px=None if len(segment_sizes) > 1 else font_size,
            color_hex=color_hex,
            layer_name="reference-thumbnail",
            alignment="center",
            faux_bold=False,
            allow_auto_bold=False,
            max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            segments=segments or (),
        )
        styles.append(style)
    return styles


def extract_top_reference_line_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int,
) -> list[TnLineStyle]:
    """Derive line styles from the top English text region only."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), template_size)
    top_bands, bottom_bands = _split_top_bottom_bands(bands, template_size[1])
    selected = _select_bands_for_caption(top_bands, template_size, caption_line_count)
    if not selected:
        return []

    if _is_enlightenment_top_layout(selected) and len(bottom_bands) >= 2:
        return _layout_enlightenment_line_styles(selected, bottom_bands, template_size)

    return _layout_reference_line_styles(selected, template_size)


def _enlightenment_bottom_cover_bands(
    bottom_bands: list[tuple[int, int, int, int, str, str | None]],
    height: int,
) -> list[tuple[int, int, int, int, str, str | None]]:
    cover_start = int(height * ENLIGHTENMENT_BOTTOM_COVER_MIN_RATIO)
    return [band for band in bottom_bands if band[1] >= cover_start]


def cover_split_reference_text(
    image: Image.Image,
    *,
    solid_fill: bool = True,
) -> Image.Image:
    """Cover top and bottom English text, leaving the middle image untouched."""
    aligned = image.convert("RGB")
    bands = _filter_bands(_text_bands(aligned, scan_start_ratio=0.0), aligned.size)
    top, bottom = _split_top_bottom_bands(bands, aligned.size[1])
    height = aligned.size[1]
    is_enlightenment = False
    top_to_cover = top
    top_cover_margin = max(12, int(height * 0.02))
    top_cover_bottom_margin: int | None = None
    top_cover_shift_y = 0
    if len(top) >= 4:
        selected_top = _select_bands_for_caption(top, aligned.size, 4)
        is_enlightenment = _is_enlightenment_top_layout(selected_top)
        if is_enlightenment:
            top_to_cover = selected_top
            top_cover_margin, top_cover_bottom_margin, top_cover_shift_y = (
                _enlightenment_top_cover_margins(height)
            )

    result = _cover_band_group(
        image,
        top_to_cover,
        solid_fill=solid_fill,
        margin_y_top=top_cover_margin,
        margin_y_bottom=top_cover_bottom_margin,
        cover_width_ratio=ENLIGHTENMENT_TOP_COVER_WIDTH_RATIO if is_enlightenment else None,
        shift_y=top_cover_shift_y,
    )

    bottom_to_cover = bottom
    bottom_margin_y_top = max(4, int(height * SPLIT_BOTTOM_COVER_TOP_MARGIN_RATIO))
    bottom_margin_y_bottom = max(24, int(height * SPLIT_BOTTOM_COVER_BOTTOM_MARGIN_RATIO))
    if is_enlightenment:
        filtered = _enlightenment_bottom_cover_bands(bottom, height)
        if filtered:
            bottom_to_cover = filtered
            bottom_margin_y_top = max(8, int(height * ENLIGHTENMENT_BOTTOM_COVER_TOP_MARGIN_RATIO))
            bottom_margin_y_bottom = max(
                28, int(height * ENLIGHTENMENT_BOTTOM_COVER_BOTTOM_MARGIN_RATIO)
            )

    return _cover_band_group(
        result,
        bottom_to_cover,
        solid_fill=solid_fill,
        margin_y_top=bottom_margin_y_top,
        margin_y_bottom=bottom_margin_y_bottom,
    )


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
    max_body_width = max(1, width - 2 * TEXT_EDGE_MARGIN_PX)
    body_width = min(int(width * 0.88), max_body_width)
    left = max(TEXT_EDGE_MARGIN_PX, (width - body_width) // 2)
    right = min(width - TEXT_EDGE_MARGIN_PX, left + body_width)

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


def _font_size_from_band_height(band_height: int, *, scale: float = 1.0) -> float:
    return max(24.0, band_height * 0.82 * REFERENCE_FONT_SCALE * scale)


def _layout_mystic_musings_three_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    """Layout reordered subtitle / program / title lines for Mystic Musings captions."""
    if len(bands) != 3:
        return _layout_reference_line_styles(bands, template_size)

    width, height = template_size
    body_width = int(width * 0.88)
    left = max(0, (width - body_width) // 2)
    right = min(width, left + body_width)

    font_sizes = [
        _font_size_from_band_height(max(1, bands[0][3] - bands[0][1]), scale=MYSTIC_MUSINGS_LINE1_FONT_SCALE),
        _font_size_from_band_height(max(1, bands[1][3] - bands[1][1]), scale=MYSTIC_MUSINGS_LINE2_FONT_SCALE),
        _font_size_from_band_height(max(1, bands[2][3] - bands[2][1]), scale=MYSTIC_MUSINGS_LINE3_FONT_SCALE),
    ]
    line_heights = [max(1, int(round(size * 1.14))) for size in font_sizes]
    normal_gap = max(3, int(round(font_sizes[1] * REFERENCE_BODY_LINE_GAP_FACTOR)))
    extra_gap = max(18, int(round(font_sizes[0] * MYSTIC_MUSINGS_FIRST_GAP_FACTOR)))

    body_top = min(band[1] for band in bands)
    body_bottom = max(band[3] for band in bands)
    total_height = (
        line_heights[0]
        + extra_gap
        + line_heights[1]
        + normal_gap
        + line_heights[2]
    )
    start_y = max(0, (body_top + body_bottom) // 2 - total_height // 2)

    styles: list[TnLineStyle] = []
    cursor = start_y
    for index, (band, font_size, line_height) in enumerate(
        zip(bands, font_sizes, line_heights, strict=True)
    ):
        top = cursor
        bottom = min(height, top + line_height)
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(left, top, right, bottom),
                font_size_px=font_size,
                color_hex=band[4],
                layer_name="reference-thumbnail",
                alignment="center",
                faux_bold=False,
                max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            )
        )
        if index == 0:
            cursor = bottom + extra_gap
        elif index == 1:
            cursor = bottom + normal_gap
        else:
            cursor = bottom

    return [
        styles[0],
        replace(styles[1], flanking_line_span_style_index=2),
        styles[2],
    ]


def _layout_mystic_musings_four_line_styles(
    bands: list[tuple[int, int, int, int, str, str | None]],
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    """Layout subtitle / program / split title lines for Mystic Musings captions."""
    if len(bands) != 4:
        return _layout_reference_line_styles(bands, template_size)

    width, height = template_size
    body_width = int(width * 0.88)
    left = max(0, (width - body_width) // 2)
    right = min(width, left + body_width)

    title_band_height = max(1, bands[2][3] - bands[2][1])
    font_sizes = [
        _font_size_from_band_height(max(1, bands[0][3] - bands[0][1]), scale=MYSTIC_MUSINGS_LINE1_FONT_SCALE),
        _font_size_from_band_height(max(1, bands[1][3] - bands[1][1]), scale=MYSTIC_MUSINGS_LINE2_FONT_SCALE),
        _font_size_from_band_height(title_band_height, scale=MYSTIC_MUSINGS_TITLE_LINE_FONT_SCALE),
        _font_size_from_band_height(title_band_height, scale=MYSTIC_MUSINGS_TITLE_LINE_FONT_SCALE),
    ]
    line_heights = [max(1, int(round(size * 1.14))) for size in font_sizes]
    normal_gap = max(3, int(round(font_sizes[1] * REFERENCE_BODY_LINE_GAP_FACTOR)))
    title_gap = max(2, int(round(font_sizes[2] * MYSTIC_MUSINGS_TITLE_LINE_GAP_FACTOR)))
    extra_gap = max(18, int(round(font_sizes[0] * MYSTIC_MUSINGS_FIRST_GAP_FACTOR)))

    body_top = min(band[1] for band in bands[:3])
    body_bottom = max(band[3] for band in bands[:3])
    total_height = (
        line_heights[0]
        + extra_gap
        + line_heights[1]
        + normal_gap
        + line_heights[2]
        + title_gap
        + line_heights[3]
    )
    start_y = max(0, (body_top + body_bottom) // 2 - total_height // 2)

    gap_after = (extra_gap, normal_gap, title_gap, 0)
    styles: list[TnLineStyle] = []
    cursor = start_y
    for index, (band, font_size, line_height) in enumerate(
        zip(bands, font_sizes, line_heights, strict=True)
    ):
        top = cursor
        bottom = min(height, top + line_height)
        styles.append(
            TnLineStyle(
                placeholder_text="reference-line",
                rendered_text="reference-line",
                bbox=(left, top, right, bottom),
                font_size_px=font_size,
                color_hex=band[4],
                layer_name="reference-thumbnail",
                alignment="center",
                faux_bold=False,
                max_grow_factor=REFERENCE_MAX_GROW_FACTOR,
            )
        )
        cursor = bottom + gap_after[index]

    return [
        styles[0],
        replace(styles[1], flanking_line_span_style_index=2),
        styles[2],
        replace(styles[3], matched_font_size_style_index=2),
    ]


def extract_reordered_mystic_musings_reference_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
    *,
    caption_line_count: int,
) -> list[TnLineStyle]:
    """Map title/program/subtitle reference bands onto reordered caption lines."""
    aligned = _resize_reference(reference.convert("RGB"), template_size)
    bands = _select_bands_for_caption(_text_bands(aligned), template_size, 3)
    if len(bands) != 3:
        return []
    if caption_line_count == 4:
        return _layout_mystic_musings_four_line_styles(
            [bands[2], bands[1], bands[0], bands[0]],
            template_size,
        )
    if caption_line_count == 3:
        return _layout_mystic_musings_three_line_styles(
            [bands[2], bands[1], bands[0]],
            template_size,
        )
    return []


def extract_reordered_three_line_reference_styles(
    reference: Image.Image,
    template_size: tuple[int, int],
) -> list[TnLineStyle]:
    """Map title/program/subtitle reference bands onto reordered caption lines."""
    return extract_reordered_mystic_musings_reference_styles(
        reference,
        template_size,
        caption_line_count=3,
    )


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
