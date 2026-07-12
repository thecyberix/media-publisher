from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from media_publisher.sources.quote_layouts import (
    QuoteLayoutConfig,
    QuoteLayoutEntry,
    QuoteLayoutError,
)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError as exc:  # pragma: no cover - optional at import time
    raise QuoteLayoutError(
        "Quote rendering requires Pillow. Install with: pip install pillow"
    ) from exc


DEFAULT_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/times.ttf"),
    Path("C:/Windows/Fonts/Times New Roman.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf"),
    Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
)

WATERMARK_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
)

WATERMARK_FONT_BOLD_CANDIDATES = (
    Path("C:/Windows/Fonts/segoeuib.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
)


class QuoteRenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuoteRenderPlan:
    layout_key: str
    lines: tuple[str, ...]
    font_size_px: int


def resolve_font_path(
    explicit: Path | None = None,
    *,
    candidates: tuple[Path, ...] = DEFAULT_FONT_CANDIDATES,
    label: str = "serif font",
) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise QuoteRenderError(f"Font file not found: {explicit}")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise QuoteRenderError(
        f"Could not locate a {label}. Install Times New Roman or pass --quote-font."
    )


def load_font(size_px: int, *, font_path: Path | None = None) -> ImageFont.FreeTypeFont:
    path = resolve_font_path(font_path)
    return ImageFont.truetype(str(path), size=size_px)


def _measure_text(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    image = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(image)
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def wrap_quote_text(
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    clean = re.sub(r"\s+", " ", text.strip())
    if not clean:
        return []

    words = clean.split(" ")
    lines: list[str] = []
    current: list[str] = []

    for word in words:
        candidate = " ".join(current + [word]) if current else word
        width, _ = _measure_text(font, candidate)
        if width <= max_width or not current:
            current.append(word)
            continue
        lines.append(" ".join(current))
        current = [word]

    if current:
        lines.append(" ".join(current))
    return lines


def _line_span_width(
    font: ImageFont.FreeTypeFont, words: list[str], start: int, end: int
) -> int:
    width, _ = _measure_text(font, " ".join(words[start:end]))
    return width


def wrap_quote_text_balanced(
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    line_count: int,
) -> list[str]:
    clean = re.sub(r"\s+", " ", text.strip())
    if not clean:
        return []
    if line_count <= 1:
        return [clean]

    words = clean.split(" ")
    word_count = len(words)
    if line_count >= word_count:
        return words

    total_width, _ = _measure_text(font, clean)
    target_width = total_width / line_count

    inf = 10**18
    dp = [[inf] * (word_count + 1) for _ in range(line_count + 1)]
    parent: list[list[tuple[int, int] | None]] = [
        [None] * (word_count + 1) for _ in range(line_count + 1)
    ]
    dp[0][0] = 0

    for lines_used in range(1, line_count + 1):
        for end in range(lines_used, word_count + 1):
            for start in range(lines_used - 1, end):
                if dp[lines_used - 1][start] == inf:
                    continue
                width = _line_span_width(font, words, start, end)
                if width > max_width:
                    continue
                cost = dp[lines_used - 1][start] + (width - target_width) ** 2
                if cost < dp[lines_used][end]:
                    dp[lines_used][end] = cost
                    parent[lines_used][end] = (lines_used - 1, start)

    if dp[line_count][word_count] == inf:
        return wrap_quote_text(clean, font=font, max_width=max_width)

    lines_rev: list[str] = []
    lines_used = line_count
    end = word_count
    while lines_used > 0 and end > 0:
        prev = parent[lines_used][end]
        if prev is None:
            return wrap_quote_text(clean, font=font, max_width=max_width)
        prev_lines, start = prev
        lines_rev.append(" ".join(words[start:end]))
        lines_used, end = prev_lines, start
    lines_rev.reverse()
    return lines_rev


def _wrap_for_plan(
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    line_count: int | None = None,
) -> list[str]:
    if line_count is not None and line_count > 1:
        balanced = wrap_quote_text_balanced(
            text,
            font=font,
            max_width=max_width,
            line_count=line_count,
        )
        if len(balanced) == line_count:
            return balanced
    return wrap_quote_text(text, font=font, max_width=max_width)


def _layout_font_ceiling(layout: QuoteLayoutEntry) -> int:
    return layout.font_size_px + 2


def select_render_plan(
    layout_config: QuoteLayoutConfig,
    text: str,
    *,
    font_path: Path | None = None,
) -> QuoteRenderPlan:
    clean = text.strip()
    if not clean:
        raise QuoteRenderError("Quote text is empty")

    for target_key in ("4", "3", "2", "1"):
        if target_key not in layout_config.layouts:
            continue
        layout = layout_config.layouts[target_key]
        max_lines = int(target_key)
        for font_size in range(_layout_font_ceiling(layout), 11, -1):
            font = load_font(font_size, font_path=font_path)
            lines = wrap_quote_text(
                clean,
                font=font,
                max_width=layout.text_box["width"],
            )
            if lines and len(lines) == max_lines:
                final_lines = _wrap_for_plan(
                    clean,
                    font=font,
                    max_width=layout.text_box["width"],
                    line_count=max_lines,
                )
                return QuoteRenderPlan(
                    layout_key=target_key,
                    lines=tuple(final_lines),
                    font_size_px=font_size,
                )

    for target_key in ("4", "3", "2", "1"):
        if target_key not in layout_config.layouts:
            continue
        layout = layout_config.layouts[target_key]
        max_lines = int(target_key)
        for font_size in range(_layout_font_ceiling(layout), 11, -1):
            font = load_font(font_size, font_path=font_path)
            lines = wrap_quote_text(
                clean,
                font=font,
                max_width=layout.text_box["width"],
            )
            if lines and len(lines) <= max_lines:
                line_count = len(lines)
                final_lines = _wrap_for_plan(
                    clean,
                    font=font,
                    max_width=layout.text_box["width"],
                    line_count=line_count if line_count > 1 else None,
                )
                return QuoteRenderPlan(
                    layout_key=target_key,
                    lines=tuple(final_lines),
                    font_size_px=font_size,
                )

    raise QuoteRenderError(
        f"Quote text does not fit the 4-line layout for variant {layout_config.variant!r}"
    )


def cover_crop(image: Image.Image, width: int, height: int) -> Image.Image:
    source = image.convert("RGB")
    source_ratio = source.width / source.height
    target_ratio = width / height
    if source_ratio > target_ratio:
        new_height = height
        new_width = round(height * source_ratio)
    else:
        new_width = width
        new_height = round(width / source_ratio)
    resized = source.resize((new_width, new_height), Image.Resampling.LANCZOS)
    left = max(0, (new_width - width) // 2)
    top = max(0, (new_height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _count_signature_ink_pixels(
    pixels, y: int, *, x_min: int, x_max: int, threshold: int = 130
) -> int:
    return sum(
        1
        for x in range(x_min, x_max)
        if pixels[x, y][0] < threshold
        and pixels[x, y][1] < threshold
        and pixels[x, y][2] < threshold
    )


def _find_signature_top_px(template: Image.Image, *, panel_top_px: int) -> int:
    pixels = template.load()
    width, height = template.size
    x_min = width // 4
    x_max = (3 * width) // 4
    matched_rows: list[int] = []
    for y in range(height - 12, panel_top_px + 200, -1):
        xs = [
            x
            for x in range(x_min, x_max)
            if pixels[x, y][0] < 85 and pixels[x, y][1] < 85 and pixels[x, y][2] < 85
        ]
        if len(xs) >= 8 and max(xs) - min(xs) + 1 >= 80:
            matched_rows.append(y)
    if not matched_rows:
        return height - 120
    block_bottom = max(matched_rows)
    block_top = min(y for y in matched_rows if block_bottom - y <= 80)

    # Include thin flourishes above the main signature strokes. The white clear
    # zone must stop before this top edge, and the template paste must start here.
    extended_top = block_top
    empty_rows = 0
    for y in range(block_top - 1, max(block_top - 80, panel_top_px + 160), -1):
        count = _count_signature_ink_pixels(pixels, y, x_min=x_min, x_max=x_max)
        if count >= 40:
            break
        if count == 0:
            empty_rows += 1
            if empty_rows > 12:
                break
            continue
        empty_rows = 0
        extended_top = y
    return extended_top


def _quote_clear_bottom_px(
    plan: QuoteRenderPlan,
    layout: QuoteLayoutEntry,
) -> int:
    last_center = layout.line_centers_px[len(plan.lines) - 1]
    return int(math.ceil(last_center + plan.font_size_px * 0.55 + 12))


def _clear_placeholder_text(
    image: Image.Image,
    *,
    panel_top_px: int,
    signature_top_px: int,
    quote_clear_bottom_px: int | None = None,
    clear_above_signature_px: int = 48,
) -> None:
    draw = ImageDraw.Draw(image)
    clear_bottom = max(panel_top_px + 46, quote_clear_bottom_px or 0)
    clear_bottom = min(clear_bottom, signature_top_px - clear_above_signature_px)
    draw.rectangle(
        [
            0,
            panel_top_px + 46,
            image.width,
            clear_bottom,
        ],
        fill="white",
    )


def _paste_signature_from_template(
    image: Image.Image,
    template: Image.Image,
    *,
    signature_top_px: int,
    paste_padding_px: int = 12,
) -> None:
    top = max(0, min(signature_top_px - paste_padding_px, template.height - 1))
    image.paste(template.crop((0, top, template.width, template.height)), (0, top))


def _is_quote_icon_pixel(red: int, green: int, blue: int) -> bool:
    is_white = red > 235 and green > 235 and blue > 235
    is_gray = 100 < red < 215 and abs(red - green) < 18 and abs(green - blue) < 18
    is_shadow = 165 < red < 235 and abs(red - green) < 10 and abs(green - blue) < 10
    return is_white or is_gray or is_shadow


def _build_quote_icon_overlay(
    template: Image.Image,
    *,
    center_x: int,
    center_y: int,
    radius: int,
) -> Image.Image:
    overlay = Image.new("RGBA", template.size, (0, 0, 0, 0))
    source = template.load()
    target = overlay.load()
    margin = 8
    outer = radius + margin
    y_min = max(0, center_y - outer)
    y_max = min(template.height, center_y + outer)
    x_min = max(0, center_x - outer)
    x_max = min(template.width, center_x + outer)
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            if math.hypot(x - center_x, y - center_y) > outer:
                continue
            red, green, blue = source[x, y]
            if not _is_quote_icon_pixel(red, green, blue):
                continue
            target[x, y] = (red, green, blue, 255)
    return overlay


def _paste_quote_icon_overlay(
    image: Image.Image,
    template: Image.Image,
    *,
    quote_icon: dict[str, object] | None,
    panel_top_px: int,
) -> Image.Image:
    icon = quote_icon if isinstance(quote_icon, dict) else {}
    center_x = int(icon.get("center_x_px", template.width // 2))
    center_y = int(icon.get("center_y_px", panel_top_px))
    radius = int(icon.get("radius_px", 52))
    overlay = _build_quote_icon_overlay(
        template,
        center_x=center_x,
        center_y=center_y,
        radius=radius,
    )
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def _draw_brand_watermark(
    image: Image.Image,
    *,
    watermark: dict[str, object] | None,
) -> None:
    if not watermark:
        return
    text = str(watermark.get("text", "")).strip()
    if not text:
        return

    font_size = int(watermark.get("font_size_px", 15))
    font_weight = str(watermark.get("font_weight", "normal")).lower()
    font_candidates = (
        WATERMARK_FONT_BOLD_CANDIDATES
        if font_weight == "bold"
        else WATERMARK_FONT_CANDIDATES
    )
    font_path = resolve_font_path(
        candidates=font_candidates,
        label="sans-serif font for watermark",
    )
    font = ImageFont.truetype(str(font_path), size=font_size)
    color = str(watermark.get("color", "#FFFFFF"))
    x = int(watermark.get("x_px", 10))
    y = int(watermark.get("y_px", 7))
    draw = ImageDraw.Draw(image)
    draw.text((x, y), text, font=font, fill=color, anchor="lt")


def _draw_centered_lines(
    image: Image.Image,
    *,
    lines: tuple[str, ...],
    layout: QuoteLayoutEntry,
    font: ImageFont.FreeTypeFont,
    color: str,
) -> None:
    draw = ImageDraw.Draw(image)
    centers = layout.line_centers_px[: len(lines)]
    if len(centers) < len(lines):
        box = layout.text_box
        top = box["y"]
        spacing = layout.line_spacing_px
        centers = [top + spacing * index for index in range(len(lines))]

    x_center = layout.text_box["x"] + layout.text_box["width"] // 2
    pad_y = 4
    line_left = layout.text_box["x"]
    line_right = layout.text_box["x"] + layout.text_box["width"]
    for line, y_center in zip(lines, centers):
        bbox = font.getbbox(line)
        top = y_center + bbox[1] - pad_y
        bottom = y_center + bbox[3] + pad_y
        draw.rectangle([line_left, top, line_right, bottom], fill="white")
        draw.text((x_center, y_center), line, font=font, fill=color, anchor="mm")


def render_quote_image(
    *,
    background_path: Path,
    layout_config: QuoteLayoutConfig,
    plan: QuoteRenderPlan,
    font_path: Path | None = None,
) -> Image.Image:
    layout = layout_config.layouts[plan.layout_key]
    template_path = layout_config.template_path(plan.layout_key)
    if not template_path.is_file():
        raise QuoteRenderError(f"Template image not found: {template_path}")
    if not background_path.is_file():
        raise QuoteRenderError(f"Background image not found: {background_path}")

    template = Image.open(template_path).convert("RGB")
    background = Image.open(background_path)
    photo = cover_crop(background, template.width, layout_config.panel_top_px)
    signature_top_px = _find_signature_top_px(
        template,
        panel_top_px=layout_config.panel_top_px,
    )

    result = template.copy()
    result.paste(photo, (0, 0))
    _draw_brand_watermark(result, watermark=layout_config.brand_watermark)
    _clear_placeholder_text(
        result,
        panel_top_px=layout_config.panel_top_px,
        signature_top_px=signature_top_px,
        quote_clear_bottom_px=_quote_clear_bottom_px(plan, layout),
    )
    result = _paste_quote_icon_overlay(
        result,
        template,
        quote_icon=layout_config.quote_icon,
        panel_top_px=layout_config.panel_top_px,
    )

    font = load_font(plan.font_size_px, font_path=font_path)
    color = str(layout_config.typography.get("color", "#000000"))
    _draw_centered_lines(
        result,
        lines=plan.lines,
        layout=layout,
        font=font,
        color=color,
    )
    _paste_signature_from_template(
        result,
        template,
        signature_top_px=signature_top_px,
    )
    return result


def save_quote_image(image: Image.Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="JPEG", quality=92, optimize=True)
    return destination
