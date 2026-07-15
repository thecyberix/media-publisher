from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from media_publisher.sources.quote_renderer import (
    QuoteRenderError,
    _measure_text,
    load_font,
    resolve_font_path,
)
from media_publisher.sources.tn_psd import TnLineStyle, TnTextSegment
from media_publisher.sources.tn_text_mapping import (
    assign_english_to_line_styles,
    apply_typography_preferences,
    consolidate_line_styles,
    prepare_layout_line_styles,
    _is_adolescence_layout,
    _is_consciousness_layout,
    _is_farm_layout,
    _is_inspiring_layout,
    _is_kailash_layout,
    _is_kailash_title,
    _is_krishna_layout,
    _is_past_layout,
    _is_shiva_layout,
    kailash_template_line_styles,
)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover
    raise QuoteRenderError(
        "TN rendering requires Pillow. Install with: pip install pillow"
    ) from exc


TN_FONT_BOLD_CANDIDATES = (
    Path("C:/Windows/Fonts/timesbd.ttf"),
    Path("C:/Windows/Fonts/timesbi.ttf"),
    Path("C:/Windows/Fonts/Times New Roman Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf"),
)

TN_FONT_REGULAR_CANDIDATES = (
    Path("C:/Windows/Fonts/times.ttf"),
    Path("C:/Windows/Fonts/Times New Roman.ttf"),
    Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
)


class TnRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class TnRenderResult:
    destination: Path
    width: int
    height: int
    line_count: int


def _bbox_size(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    left, top, right, bottom = bbox
    return right - left, bottom - top


def _is_merged_text_block(psd_size: float, box_height: int) -> bool:
    return psd_size >= 50 and box_height > psd_size * 2.5


def _should_upscale_font(psd_size: float, box_height: int) -> bool:
    if _is_merged_text_block(psd_size, box_height):
        return True
    return psd_size < box_height * 0.55


def _max_upscale_target(psd_size: float, box_height: int) -> int:
    psd_int = max(12, int(round(psd_size)))
    if _is_merged_text_block(psd_size, box_height):
        return min(int(psd_size * 1.85), int(box_height * 0.38))
    if psd_size < 22:
        return min(int(psd_size * 3.2), int(box_height * 0.62))
    if psd_size < 55:
        return min(int(psd_size * 4.0), int(box_height * 0.92))
    if _should_upscale_font(psd_size, box_height):
        return psd_int
    return int(psd_int * 1.15)


def _grow_best_font_size(
    *,
    psd_reference: int,
    max_target: int,
    fits,
) -> int:
    if not fits(psd_reference):
        for size in range(psd_reference - 1, 11, -1):
            if fits(size):
                return size
        return 12

    best_size = psd_reference
    for size in range(psd_reference + 1, max_target + 1):
        if fits(size):
            best_size = size
            continue
        break
    return best_size


def _is_light_color(color_hex: str) -> bool:
    text = color_hex.lstrip("#")
    if len(text) != 6:
        return True
    red = int(text[0:2], 16)
    green = int(text[2:4], 16)
    blue = int(text[4:6], 16)
    return red > 220 and green > 220 and blue > 220


def _font_path_for_style(
    *,
    faux_bold: bool,
    font_size_px: float,
    color_hex: str,
    font_index: int | None = None,
    allow_auto_bold: bool = True,
) -> Path:
    accent_color = not _is_light_color(color_hex) and color_hex.lower() not in {"#ff7a00", "#ef843d", "#feeea2", "#ffbb38"}
    use_bold = faux_bold
    if allow_auto_bold and not use_bold:
        use_bold = (
            font_size_px >= 72
            or accent_color
            or (font_index == 3 and font_size_px >= 30)
            or (font_index == 6 and font_size_px >= 30)
        )
    candidates = TN_FONT_BOLD_CANDIDATES if use_bold else TN_FONT_REGULAR_CANDIDATES
    label = "TN bold font" if use_bold else "TN regular font"
    return resolve_font_path(candidates=candidates, label=label)


def _effective_segments(style: TnLineStyle) -> tuple[TnTextSegment, ...]:
    if style.segments:
        return style.segments
    text = style.rendered_text.strip()
    if not text:
        return ()
    return (
        TnTextSegment(
            text=text,
            font_size_px=style.font_size_px,
            color_hex=style.color_hex,
            font_index=style.font_index,
            faux_bold=style.faux_bold,
        ),
    )


def _font_for_segment(
    segment: TnTextSegment,
    size_px: int,
    *,
    allow_auto_bold: bool = True,
) -> ImageFont.FreeTypeFont:
    font_path = _font_path_for_style(
        faux_bold=segment.faux_bold,
        font_size_px=segment.font_size_px,
        color_hex=segment.color_hex,
        font_index=segment.font_index,
        allow_auto_bold=allow_auto_bold,
    )
    return load_font(size_px, font_path=font_path)


def _line_fits(
    segments: list[TnTextSegment],
    fonts: list[ImageFont.FreeTypeFont],
    box_width: int,
    box_height: int,
) -> bool:
    total_width = 0
    max_height = 0
    for segment, font in zip(segments, fonts, strict=False):
        width, height = _measure_text(font, segment.text)
        total_width += width
        max_height = max(max_height, height)
    return total_width <= box_width * 0.98 and max_height <= box_height * 0.92


def _build_segment_fonts(
    segments: list[TnTextSegment],
    reference_size: int,
    *,
    allow_auto_bold: bool = True,
) -> list[ImageFont.FreeTypeFont]:
    max_target = max(segment.font_size_px for segment in segments)
    fonts: list[ImageFont.FreeTypeFont] = []
    for segment in segments:
        scaled_size = max(12, int(round(reference_size * segment.font_size_px / max_target)))
        fonts.append(
            _font_for_segment(segment, scaled_size, allow_auto_bold=allow_auto_bold)
        )
    return fonts


def _cap_max_target(
    psd_reference: int,
    max_target: int,
    max_grow_factor: float | None,
) -> int:
    if max_grow_factor is None:
        return max_target
    capped = max(psd_reference, int(round(psd_reference * max_grow_factor)))
    return min(max_target, capped)


def _fit_line_fonts(style: TnLineStyle) -> list[tuple[TnTextSegment, ImageFont.FreeTypeFont]]:
    segments = [segment for segment in _effective_segments(style) if segment.text]
    if not segments:
        return []

    box_width, box_height = _bbox_size(style.bbox)
    psd_size = max(segment.font_size_px for segment in segments)
    psd_reference = max(12, int(round(psd_size)))
    if style.fixed_font_size_px is not None:
        best_size = max(12, int(round(style.fixed_font_size_px)))
    else:
        max_target = _cap_max_target(
            psd_reference,
            _max_upscale_target(psd_size, box_height),
            style.max_grow_factor,
        )

        def fits(size: int) -> bool:
            fonts = _build_segment_fonts(
                segments,
                size,
                allow_auto_bold=style.allow_auto_bold,
            )
            return _line_fits(segments, fonts, box_width, box_height)

        best_size = _grow_best_font_size(
            psd_reference=psd_reference,
            max_target=max_target,
            fits=fits,
        )

    fonts = _build_segment_fonts(
        segments,
        best_size,
        allow_auto_bold=style.allow_auto_bold,
    )
    return list(zip(segments, fonts, strict=False))


def _block_lines_for_text(text: str) -> list[str] | None:
    cleaned = text.strip()
    lowered = cleaned.casefold()
    marker = " a miracle"
    if marker in lowered:
        index = lowered.index(marker)
        return [cleaned[:index].strip(), cleaned[index + 1 :].strip()]
    if " & " in cleaned:
        head, tail = cleaned.split(" & ", 1)
        return [head.strip(), f"& {tail.strip()}"]
    return None


def _block_line_gap_factor(style: TnLineStyle) -> float:
    if style.stacked_line_gap_factor is not None:
        return style.stacked_line_gap_factor
    if style.block_line_parts:
        return 0.12
    return 0.15


def _block_line_gap(size: int, style: TnLineStyle) -> int:
    return max(4, int(size * _block_line_gap_factor(style)))


def _segment_row_width(
    segments: tuple[TnTextSegment, ...],
    size: int,
    *,
    allow_auto_bold: bool,
) -> int:
    if not segments:
        return 0
    fonts = _build_segment_fonts(list(segments), size, allow_auto_bold=allow_auto_bold)
    return sum(
        _measure_text(font, segment.text)[0]
        for segment, font in zip(segments, fonts, strict=False)
    )


def _segment_row_height(
    segments: tuple[TnTextSegment, ...],
    size: int,
    *,
    allow_auto_bold: bool,
) -> int:
    fonts = _build_segment_fonts(list(segments), size, allow_auto_bold=allow_auto_bold)
    if not fonts:
        return 0
    return max(
        _measure_text(font, segment.text)[1]
        for segment, font in zip(segments, fonts, strict=False)
    )


def _best_size_for_target_width(
    segments: tuple[TnTextSegment, ...],
    target_width: int,
    max_size: int,
    *,
    allow_auto_bold: bool,
) -> int:
    best_size = 12
    best_delta = float("inf")
    for size in range(12, max_size + 1):
        width = _segment_row_width(segments, size, allow_auto_bold=allow_auto_bold)
        delta = abs(width - target_width)
        if delta < best_delta:
            best_delta = delta
            best_size = size
        if width > target_width:
            break
    return best_size


def _fit_matched_stacked_font_sizes(
    style: TnLineStyle,
    lines: list[str],
    *,
    width_target: int | None = None,
    height_fill: float = 0.92,
) -> list[int]:
    row_segments = [_segments_for_block_part(style, line) for line in lines]
    if not row_segments or not row_segments[0]:
        return [12] * len(lines)

    box_width, box_height = _bbox_size(style.bbox)
    psd_reference = max(12, int(round(style.font_size_px)))
    max_target = _cap_max_target(
        psd_reference,
        _max_upscale_target(style.font_size_px, box_height),
        style.max_grow_factor,
    )

    best_sizes: list[int] | None = None
    best_score = -1

    if width_target is not None:
        target = min(width_target, int(box_width * 0.98))
        size1 = _best_size_for_target_width(
            row_segments[1],
            target,
            max_target,
            allow_auto_bold=style.allow_auto_bold,
        )
        size0_fit = _best_size_for_target_width(
            row_segments[0],
            target,
            max_target,
            allow_auto_bold=style.allow_auto_bold,
        )
        if size0_fit > size1:
            width0 = _segment_row_width(
                row_segments[0],
                size0_fit,
                allow_auto_bold=style.allow_auto_bold,
            )
            width1 = _segment_row_width(
                row_segments[1],
                size1,
                allow_auto_bold=style.allow_auto_bold,
            )
            if max(abs(width0 - target), abs(width1 - target)) <= 4:
                row_heights = [
                    _segment_row_height(row_segments[0], size0_fit, allow_auto_bold=style.allow_auto_bold),
                    _segment_row_height(row_segments[1], size1, allow_auto_bold=style.allow_auto_bold),
                ]
                gap = _block_line_gap(max(size0_fit, size1), style)
                if sum(row_heights) + gap <= box_height * height_fill:
                    return [size0_fit, size1]

    for size0 in range(max_target, psd_reference - 1, -1):
        width0 = _segment_row_width(
            row_segments[0],
            size0,
            allow_auto_bold=style.allow_auto_bold,
        )
        if width0 > box_width * 0.98:
            continue

        size1 = _best_size_for_target_width(
            row_segments[1],
            width0,
            max(12, size0 - 1),
            allow_auto_bold=style.allow_auto_bold,
        )
        if size1 >= size0:
            continue

        width1 = _segment_row_width(
            row_segments[1],
            size1,
            allow_auto_bold=style.allow_auto_bold,
        )
        if abs(width1 - width0) > 4:
            continue

        row_heights = [
            _segment_row_height(row_segments[0], size0, allow_auto_bold=style.allow_auto_bold),
            _segment_row_height(row_segments[1], size1, allow_auto_bold=style.allow_auto_bold),
        ]
        gap = _block_line_gap(max(size0, size1), style)
        total_height = sum(row_heights) + gap
        if total_height > box_height * height_fill:
            continue

        if size0 > best_score:
            best_score = size0
            best_sizes = [size0, size1]

    if best_sizes is not None:
        return best_sizes

    fallback = _fit_block_font_size(style, lines)
    return [fallback] * len(lines)


def _fit_block_font_size(style: TnLineStyle, lines: list[str]) -> int:
    segments = [segment for segment in _effective_segments(style) if segment.text]
    if not segments:
        return 12
    segment = segments[0]
    box_width, box_height = _bbox_size(style.bbox)
    psd_reference = max(12, int(round(style.font_size_px)))
    max_target = _cap_max_target(
        psd_reference,
        _max_upscale_target(style.font_size_px, box_height),
        style.max_grow_factor,
    )

    def block_fits(size: int) -> bool:
        font = _font_for_segment(
            segment,
            size,
            allow_auto_bold=style.allow_auto_bold,
        )
        widths: list[int] = []
        heights: list[int] = []
        for line in lines:
            width, height = _measure_text(font, line)
            widths.append(width)
            heights.append(height)
        total_height = sum(heights) + int((len(lines) - 1) * size * _block_line_gap_factor(style))
        return max(widths) <= box_width * 0.98 and total_height <= box_height * 0.92

    if not block_fits(psd_reference):
        for size in range(psd_reference - 1, 11, -1):
            if block_fits(size):
                return size
        return 12

    return _grow_best_font_size(
        psd_reference=psd_reference,
        max_target=max_target,
        fits=block_fits,
    )


def _segments_for_block_part(
    style: TnLineStyle,
    part_text: str,
) -> tuple[TnTextSegment, ...]:
    segments = [segment for segment in _effective_segments(style) if segment.text.strip()]
    if not segments:
        return ()
    if len(segments) == 1:
        return (TnTextSegment(
            text=part_text,
            font_size_px=segments[0].font_size_px,
            color_hex=segments[0].color_hex,
            font_index=segments[0].font_index,
            faux_bold=segments[0].faux_bold,
        ),)

    matched: list[TnTextSegment] = []
    remaining = part_text
    for segment in segments:
        chunk = segment.text.strip()
        if not chunk:
            continue
        if chunk.casefold() in remaining.casefold():
            start = remaining.casefold().index(chunk.casefold())
            prefix = remaining[:start]
            if prefix and matched:
                matched[-1] = TnTextSegment(
                    text=matched[-1].text + prefix,
                    font_size_px=matched[-1].font_size_px,
                    color_hex=matched[-1].color_hex,
                    font_index=matched[-1].font_index,
                    faux_bold=matched[-1].faux_bold,
                )
            elif prefix:
                matched.append(
                    TnTextSegment(
                        text=prefix,
                        font_size_px=segment.font_size_px,
                        color_hex=segment.color_hex,
                        font_index=segment.font_index,
                        faux_bold=segment.faux_bold,
                    )
                )
            end = start + len(chunk)
            matched.append(
                TnTextSegment(
                    text=remaining[start:end],
                    font_size_px=segment.font_size_px,
                    color_hex=segment.color_hex,
                    font_index=segment.font_index,
                    faux_bold=segment.faux_bold,
                )
            )
            remaining = remaining[end:]
    if remaining.strip():
        tail = segments[-1]
        matched.append(
            TnTextSegment(
                text=remaining,
                font_size_px=tail.font_size_px,
                color_hex=tail.color_hex,
                font_index=tail.font_index,
                faux_bold=tail.faux_bold,
            )
        )
    if not matched:
        primary = segments[0]
        return (
            TnTextSegment(
                text=part_text,
                font_size_px=primary.font_size_px,
                color_hex=primary.color_hex,
                font_index=primary.font_index,
                faux_bold=primary.faux_bold,
            ),
        )
    return tuple(matched)


def _segment_row_text_bbox(
    draw: ImageDraw.ImageDraw,
    segments: tuple[TnTextSegment, ...],
    fonts: list[ImageFont.FreeTypeFont],
    x: int,
    y: int,
) -> tuple[int, int, int, int]:
    left = top = right = bottom = 0
    cursor = x
    for index, (segment, font) in enumerate(zip(segments, fonts, strict=False)):
        bbox = draw.textbbox((cursor, y), segment.text, font=font, anchor="lm")
        if index == 0:
            left, top, right, bottom = bbox
        else:
            left = min(left, bbox[0])
            top = min(top, bbox[1])
            right = max(right, bbox[2])
            bottom = max(bottom, bbox[3])
        cursor += _measure_text(font, segment.text)[0]
    return left, top, right, bottom


def _draw_segment_row(
    image: Image.Image,
    *,
    style: TnLineStyle,
    segments: tuple[TnTextSegment, ...],
    y: int,
    reference_size: int,
    background_hex: str | None = None,
) -> int:
    if not segments:
        return 0
    left, _top, right, _bottom = style.bbox
    box_width = right - left
    alignment = style.alignment or "center"
    fonts = _build_segment_fonts(
        list(segments),
        reference_size,
        allow_auto_bold=style.allow_auto_bold,
    )
    total_width = sum(
        _measure_text(font, segment.text)[0]
        for segment, font in zip(segments, fonts, strict=False)
    )
    max_height = max(
        _measure_text(font, segment.text)[1]
        for segment, font in zip(segments, fonts, strict=False)
    )
    if alignment == "right":
        x = right - total_width
    elif alignment == "left":
        x = left
    else:
        x = left + max(0, (box_width - total_width) // 2)

    draw = ImageDraw.Draw(image)
    if background_hex:
        pad_x = max(4, int(reference_size * 0.08))
        pad_y_top = max(2, int(reference_size * 0.02))
        pad_y_bottom = max(1, int(reference_size * 0.01))
        text_left, text_top, text_right, text_bottom = _segment_row_text_bbox(
            draw,
            segments,
            fonts,
            x,
            y,
        )
        rect = (
            text_left - pad_x,
            text_top - pad_y_top,
            text_right + pad_x,
            text_bottom + pad_y_bottom,
        )
        draw.rectangle(rect, fill=background_hex)

    for segment, font in zip(segments, fonts, strict=False):
        draw.text(
            (x, y),
            segment.text,
            font=font,
            fill=segment.color_hex,
            anchor="lm",
        )
        x += _measure_text(font, segment.text)[0]
    return max_height


def _draw_stacked_lines(image: Image.Image, style: TnLineStyle, lines: list[str]) -> None:
    if not lines:
        return
    left, top, right, bottom = style.bbox
    box_height = bottom - top
    if style.stacked_line_font_sizes:
        row_sizes = list(style.stacked_line_font_sizes)
    elif style.stacked_line_match_widths and len(lines) > 1:
        row_sizes = _fit_matched_stacked_font_sizes(style, lines)
    else:
        font_size = _fit_block_font_size(style, lines)
        row_sizes = [font_size] * len(lines)
    gap = _block_line_gap(max(row_sizes), style)
    row_heights: list[int] = []
    for line, row_size in zip(lines, row_sizes, strict=False):
        segments = _segments_for_block_part(style, line)
        if not segments:
            continue
        row_heights.append(
            _segment_row_height(
                segments,
                row_size,
                allow_auto_bold=style.allow_auto_bold,
            )
        )
    total_height = sum(row_heights) + gap * (len(row_heights) - 1)
    y = top + max(0, (box_height - total_height) // 2)
    backgrounds = style.stacked_line_backgrounds
    for index, (line, row_height, row_size) in enumerate(
        zip(lines, row_heights, row_sizes, strict=False)
    ):
        segments = _segments_for_block_part(style, line)
        background_hex = backgrounds[index] if index < len(backgrounds) else None
        drawn = _draw_segment_row(
            image,
            style=style,
            segments=segments,
            y=y + row_height // 2,
            reference_size=row_size,
            background_hex=background_hex,
        )
        y += max(drawn, row_height) + gap


def _draw_block_lines(image: Image.Image, style: TnLineStyle, lines: list[str]) -> None:
    segments = [segment for segment in _effective_segments(style) if segment.text]
    if not segments:
        return
    segment = segments[0]
    font_size = _fit_block_font_size(style, lines)
    font = _font_for_segment(
        segment,
        font_size,
        allow_auto_bold=style.allow_auto_bold,
    )
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = style.bbox
    box_width = right - left
    box_height = bottom - top
    alignment = style.alignment or "center"

    line_metrics = [_measure_text(font, line) for line in lines]
    gap = int(font_size * 0.15)
    total_height = sum(height for _, height in line_metrics) + gap * (len(lines) - 1)
    y = top + max(0, (box_height - total_height) // 2)

    for line, (line_width, line_height) in zip(lines, line_metrics, strict=False):
        if alignment == "right":
            x = right - line_width
        elif alignment == "left":
            x = left
        else:
            x = left + max(0, (box_width - line_width) // 2)
        draw.text(
            (x, y),
            line,
            font=font,
            fill=segment.color_hex,
            anchor="lt",
        )
        y += line_height + gap


def _line_render_width(style: TnLineStyle) -> int:
    segment_fonts = _fit_line_fonts(style)
    if not segment_fonts:
        return 0
    return sum(_measure_text(font, segment.text)[0] for segment, font in segment_fonts)


def _draw_flanking_lines(
    draw: ImageDraw.ImageDraw,
    *,
    center_y: int,
    text_left: int,
    text_right: int,
    span_width: int,
    color: str,
    gap_px: int = 10,
    thickness: int = 2,
) -> None:
    text_width = text_right - text_left
    if span_width <= text_width + gap_px * 2:
        return
    center_x = (text_left + text_right) // 2
    span_left = center_x - span_width // 2
    span_right = span_left + span_width
    draw.line(
        [(span_left, center_y), (text_left - gap_px, center_y)],
        fill=color,
        width=thickness,
    )
    draw.line(
        [(text_right + gap_px, center_y), (span_right, center_y)],
        fill=color,
        width=thickness,
    )


def _draw_line(
    image: Image.Image,
    style: TnLineStyle,
    *,
    peer_widths: dict[int, int] | None = None,
    style_index: int | None = None,
) -> None:
    if style.block_line_parts:
        _draw_stacked_lines(image, style, list(style.block_line_parts))
        return

    block_lines = _block_lines_for_text(style.rendered_text)
    box_width, box_height = _bbox_size(style.bbox)
    if (
        block_lines
        and len(block_lines) > 1
        and _is_merged_text_block(style.font_size_px, box_height)
    ):
        _draw_block_lines(image, style, block_lines)
        return

    segment_fonts = _fit_line_fonts(style)
    if not segment_fonts:
        return

    draw = ImageDraw.Draw(image)
    left, top, right, bottom = style.bbox
    box_width = right - left
    box_height = bottom - top
    font_sizes = {segment.font_size_px for segment, _ in segment_fonts}
    use_baseline = len(segment_fonts) > 1 and len(font_sizes) > 1
    if use_baseline:
        y = bottom - max(6, int(box_height * 0.12))
        anchor = "ls"
    else:
        y = top + box_height // 2
        anchor = "lm"

    total_width = sum(
        _measure_text(font, segment.text)[0] for segment, font in segment_fonts
    )
    alignment = style.alignment or "center"
    if alignment == "right":
        x = right - total_width
    elif alignment == "left":
        x = left
    else:
        x = left + max(0, (box_width - total_width) // 2)

    text_left = x
    for segment, font in segment_fonts:
        draw.text(
            (x, y),
            segment.text,
            font=font,
            fill=segment.color_hex,
            anchor=anchor,
        )
        x += _measure_text(font, segment.text)[0]
    text_right = x

    span_index = style.flanking_line_span_style_index
    if span_index is not None and peer_widths is not None:
        span_width = peer_widths.get(span_index, 0)
        if span_width <= 0 and span_index < len(peer_widths):
            span_width = peer_widths[span_index]
        if span_width > 0:
            _draw_flanking_lines(
                draw,
                center_y=y,
                text_left=text_left,
                text_right=text_right,
                span_width=span_width,
                color=segment_fonts[0][0].color_hex,
                gap_px=max(8, int(round(image.width * 0.012))),
                thickness=max(3, int(round(image.height * 0.0035))),
            )


def fallback_line_styles(width: int, height: int, english_text: str) -> list[TnLineStyle]:
    from media_publisher.sources.tn_text_mapping import english_lines_for_render

    lines = english_lines_for_render(english_text) or [english_text.strip()]
    box_height = max(120, int(height * 0.28))
    top = height - box_height - int(height * 0.05)
    slice_height = box_height / len(lines)
    result: list[TnLineStyle] = []
    for index, line in enumerate(lines):
        line_top = top + round(index * slice_height)
        line_bottom = top + round((index + 1) * slice_height)
        if index == len(lines) - 1:
            line_bottom = height - int(height * 0.04)
        result.append(
            TnLineStyle(
                placeholder_text=line,
                rendered_text=line,
                bbox=(int(width * 0.08), line_top, int(width * 0.92), line_bottom),
                font_size_px=max(28.0, height * 0.07),
                color_hex="#FFFFFF",
                layer_name="fallback",
                alignment="center",
                faux_bold=True,
            )
        )
    return result


def _stacked_block_fits(
    style: TnLineStyle,
    lines: list[str],
    row_sizes: list[int],
) -> bool:
    box_width, box_height = _bbox_size(style.bbox)
    widths: list[int] = []
    heights: list[int] = []
    for line, row_size in zip(lines, row_sizes, strict=False):
        segments = _segments_for_block_part(style, line)
        if not segments:
            continue
        widths.append(
            _segment_row_width(
                segments,
                row_size,
                allow_auto_bold=style.allow_auto_bold,
            )
        )
        heights.append(
            _segment_row_height(
                segments,
                row_size,
                allow_auto_bold=style.allow_auto_bold,
            )
        )
    gap = _block_line_gap(max(row_sizes), style)
    total_height = sum(heights) + gap * (len(heights) - 1)
    if not widths:
        return False
    return max(widths) <= box_width * 0.98 and total_height <= box_height * 0.92


def _grow_stacked_font_sizes(style: TnLineStyle, lines: list[str], base_size: int) -> list[int]:
    row_sizes = [base_size] * len(lines)
    psd_reference = max(12, int(round(style.font_size_px)))
    _, box_height = _bbox_size(style.bbox)
    max_target = _cap_max_target(
        psd_reference,
        _max_upscale_target(style.font_size_px, box_height),
        style.max_grow_factor,
    )
    while row_sizes[0] < max_target and _stacked_block_fits(
        style,
        lines,
        [row_sizes[0] + 1] * len(lines),
    ):
        row_sizes = [row_sizes[0] + 1] * len(lines)
    return row_sizes


def _style_with_stacked_font_sizes(
    style: TnLineStyle,
    row_sizes: tuple[int, ...],
) -> TnLineStyle:
    return TnLineStyle(
        placeholder_text=style.placeholder_text,
        rendered_text=style.rendered_text,
        bbox=style.bbox,
        font_size_px=style.font_size_px,
        color_hex=style.color_hex,
        font_index=style.font_index,
        layer_name=style.layer_name,
        alignment=style.alignment,
        faux_bold=style.faux_bold,
        segments=style.segments,
        max_grow_factor=style.max_grow_factor,
        allow_auto_bold=style.allow_auto_bold,
        block_line_parts=style.block_line_parts,
        stacked_line_gap_factor=style.stacked_line_gap_factor,
        fixed_font_size_px=style.fixed_font_size_px,
        stacked_line_backgrounds=style.stacked_line_backgrounds,
        stacked_line_match_widths=style.stacked_line_match_widths,
        stacked_line_font_sizes=row_sizes,
    )


def _prepare_kailash_font_sizes(assigned: list[TnLineStyle]) -> list[TnLineStyle]:
    if len(assigned) != 2:
        return assigned
    title, subtitle = assigned
    sub_lines = list(subtitle.block_line_parts)
    title_lines = list(title.block_line_parts)
    if len(sub_lines) < 1 or len(title_lines) < 2:
        return assigned

    sub_base = _fit_block_font_size(subtitle, sub_lines)
    sub_sizes = _grow_stacked_font_sizes(subtitle, sub_lines, sub_base)
    ocean_segments = _segments_for_block_part(subtitle, sub_lines[0])
    if not ocean_segments:
        return assigned
    ocean_width = _segment_row_width(
        ocean_segments,
        sub_sizes[0],
        allow_auto_bold=subtitle.allow_auto_bold,
    )
    min_with_width = int(ocean_width * 1.03)
    max_title_width = int(_bbox_size(title.bbox)[0] * 0.98)

    title_sizes: list[int] | None = None
    low = min_with_width
    high = max_title_width
    while low <= high:
        target_width = (low + high) // 2
        candidate = _fit_matched_stacked_font_sizes(
            title,
            title_lines,
            width_target=target_width,
            height_fill=0.97,
        )
        with_width = _segment_row_width(
            _segments_for_block_part(title, title_lines[1]),
            candidate[1],
            allow_auto_bold=title.allow_auto_bold,
        )
        if (
            candidate[0] > candidate[1]
            and with_width >= min_with_width
            and abs(
                _segment_row_width(
                    _segments_for_block_part(title, title_lines[0]),
                    candidate[0],
                    allow_auto_bold=title.allow_auto_bold,
                )
                - with_width
            )
            <= 4
        ):
            title_sizes = candidate
            low = target_width + 1
        else:
            high = target_width - 1

    if title_sizes is None:
        title_sizes = _fit_matched_stacked_font_sizes(title, title_lines)

    return [
        _style_with_stacked_font_sizes(title, tuple(title_sizes)),
        _style_with_stacked_font_sizes(subtitle, tuple(sub_sizes)),
    ]


def _style_with_fixed_font_size(
    style: TnLineStyle,
    size_px: int,
    *,
    bbox: tuple[int, int, int, int] | None = None,
) -> TnLineStyle:
    return TnLineStyle(
        placeholder_text=style.placeholder_text,
        rendered_text=style.rendered_text,
        bbox=bbox or style.bbox,
        font_size_px=style.font_size_px,
        color_hex=style.color_hex,
        font_index=style.font_index,
        layer_name=style.layer_name,
        alignment=style.alignment,
        faux_bold=style.faux_bold,
        segments=style.segments,
        max_grow_factor=style.max_grow_factor,
        allow_auto_bold=style.allow_auto_bold,
        block_line_parts=style.block_line_parts,
        stacked_line_gap_factor=style.stacked_line_gap_factor,
        fixed_font_size_px=float(size_px),
        stacked_line_backgrounds=style.stacked_line_backgrounds,
        stacked_line_match_widths=style.stacked_line_match_widths,
        stacked_line_font_sizes=style.stacked_line_font_sizes,
    )


CONSCIOUSNESS_CREDIT_FONT_SIZE_PX = 115
CONSCIOUSNESS_CREDIT_SHIFT_FACTOR = 0.14


def _shift_bbox_up(bbox: tuple[int, int, int, int], amount: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = bbox
    return (left, top - amount, right, bottom - amount)


def _consciousness_name_line_index(styles: list[TnLineStyle]) -> int:
    for index, style in enumerate(styles):
        if style.rendered_text.strip().casefold() in {"sadhguru", "садгуру"}:
            return index
        if style.placeholder_text.strip().casefold() == "sadhguru":
            return index
    return len(styles) - 1


def _consciousness_uniform_font_sizes(styles: list[TnLineStyle]) -> list[TnLineStyle]:
    if len(styles) != 6:
        return styles

    title_count = 3
    sadhguru_index = _consciousness_name_line_index(styles)
    fitted = _fit_line_fonts(styles[sadhguru_index])
    if not fitted:
        return styles
    title_size = fitted[0][1].size

    credit_sizes: list[int] = []
    for style in styles[title_count:]:
        fitted_credit = _fit_line_fonts(style)
        if fitted_credit:
            credit_sizes.append(fitted_credit[0][1].size)
    credit_size = CONSCIOUSNESS_CREDIT_FONT_SIZE_PX
    if credit_sizes:
        credit_size = min(credit_size, min(credit_sizes))

    _, credit_top, _, credit_bottom = styles[title_count].bbox
    credit_shift = max(12, int(round((credit_bottom - credit_top) * CONSCIOUSNESS_CREDIT_SHIFT_FACTOR)))

    result: list[TnLineStyle] = []
    for index, style in enumerate(styles):
        if index >= title_count:
            result.append(
                _style_with_fixed_font_size(
                    style,
                    credit_size,
                    bbox=_shift_bbox_up(style.bbox, credit_shift),
                )
            )
        else:
            result.append(_style_with_fixed_font_size(style, title_size))
    return result


def _apply_matched_font_sizes(styles: list[TnLineStyle]) -> list[TnLineStyle]:
    result = list(styles)
    for index, style in enumerate(styles):
        source_index = style.matched_font_size_style_index
        if source_index is None or source_index >= len(styles):
            continue
        source_fonts = _fit_line_fonts(styles[source_index])
        if not source_fonts:
            continue
        shared_size = float(source_fonts[0][1].size)
        result[index] = replace(style, fixed_font_size_px=shared_size)
    return result


def render_tn_thumbnail(
    *,
    template: Image.Image,
    english_text: str,
    line_styles: list[TnLineStyle],
    destination: Path,
    font_path: Path | None = None,
    catalog_title: str | None = None,
) -> TnRenderResult:
    if font_path is not None:
        resolve_font_path(font_path, label="TN font")

    if not line_styles:
        if catalog_title and _is_kailash_title(catalog_title):
            line_styles = kailash_template_line_styles(template.width, template.height)
        else:
            line_styles = fallback_line_styles(
                template.width,
                template.height,
                english_text,
            )

    styles = list(line_styles)
    styles = consolidate_line_styles(styles)
    farm_layout = _is_farm_layout(styles)
    krishna_layout = _is_krishna_layout(styles)
    inspiring_layout = _is_inspiring_layout(styles)
    consciousness_layout = _is_consciousness_layout(styles)
    shiva_layout = _is_shiva_layout(styles)
    past_layout = _is_past_layout(styles)
    adolescence_layout = _is_adolescence_layout(styles)
    kailash_layout = _is_kailash_layout(styles)
    styles = prepare_layout_line_styles(styles)
    assigned = assign_english_to_line_styles(english_text, styles)
    assigned = apply_typography_preferences(
        assigned,
        farm_layout=farm_layout,
        krishna_layout=krishna_layout,
        inspiring_layout=inspiring_layout,
        consciousness_layout=consciousness_layout,
        shiva_layout=shiva_layout,
        past_layout=past_layout,
        adolescence_layout=adolescence_layout,
        kailash_layout=kailash_layout,
    )
    if consciousness_layout and len(assigned) == 6:
        assigned = _consciousness_uniform_font_sizes(assigned)
    if kailash_layout:
        assigned = _prepare_kailash_font_sizes(assigned)
    if not assigned:
        raise TnRenderError("English TN text is empty")

    flanking_map = {
        index: style.flanking_line_span_style_index
        for index, style in enumerate(line_styles)
        if style.flanking_line_span_style_index is not None
    }
    matched_font_map = {
        index: style.matched_font_size_style_index
        for index, style in enumerate(line_styles)
        if style.matched_font_size_style_index is not None
    }
    if flanking_map or matched_font_map:
        assigned = [
            replace(
                style,
                flanking_line_span_style_index=flanking_map.get(index),
                matched_font_size_style_index=matched_font_map.get(index),
            )
            for index, style in enumerate(assigned)
        ]

    assigned = _apply_matched_font_sizes(assigned)

    result = template.copy()
    peer_widths = {index: _line_render_width(style) for index, style in enumerate(assigned)}
    rendered_lines = 0
    for index, style in enumerate(assigned):
        if not style.rendered_text.strip() and not any(
            segment.text for segment in style.segments
        ):
            continue
        _draw_line(result, style, peer_widths=peer_widths, style_index=index)
        rendered_lines += 1

    destination.parent.mkdir(parents=True, exist_ok=True)
    result.save(destination, format="JPEG", quality=92, optimize=True)
    return TnRenderResult(
        destination=destination,
        width=result.width,
        height=result.height,
        line_count=rendered_lines,
    )
