"""Render Mahashivratri title text onto the flat JPG template."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from PIL import Image, ImageDraw, ImageFont

TEMPLATE = PROJECT_ROOT / "downloads" / "tn-cache" / "Mahashivratri 2025.jpg"
OUTPUT = PROJECT_ROOT / "downloads" / "tn-rendered" / "Mahashivratri 2025.tn-render.jpg"

LINES = ("A Mystical", "Magical", "Night")
# Shifted up from PSD slots to sit closer to the top logo area.
LINE_BOXES = (
    (107, 270, 763, 411),
    (107, 411, 763, 552),
    (107, 552, 763, 693),
)
GRADIENT_TOP = (255, 245, 130)
GRADIENT_BOTTOM = (255, 210, 85)
FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/ariblk.ttf"),
    Path("C:/Windows/Fonts/impact.ttf"),
    Path("C:/Windows/Fonts/arialbd.ttf"),
    Path("C:/Windows/Fonts/segoeuib.ttf"),
)


def resolve_font(size_px: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if candidate.is_file():
            return ImageFont.truetype(str(candidate), size=size_px)
    raise RuntimeError("No suitable bold font found")


def _text_size(font: ImageFont.FreeTypeFont, text: str) -> tuple[int, int]:
    image = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _fit_font(text: str, box: tuple[int, int, int, int]) -> ImageFont.FreeTypeFont:
    left, top, right, bottom = box
    max_width = right - left
    max_height = bottom - top
    for size in range(130, 40, -1):
        font = resolve_font(size)
        width, height = _text_size(font, text)
        if width <= max_width and height <= max_height:
            return font
    return resolve_font(40)


def _gradient_image(width: int, height: int) -> Image.Image:
    gradient = Image.new("RGB", (width, height))
    pixels = gradient.load()
    top_r, top_g, top_b = GRADIENT_TOP
    bottom_r, bottom_g, bottom_b = GRADIENT_BOTTOM
    for y in range(height):
        ratio = y / max(height - 1, 1)
        red = round(top_r + (bottom_r - top_r) * ratio)
        green = round(top_g + (bottom_g - top_g) * ratio)
        blue = round(top_b + (bottom_b - top_b) * ratio)
        for x in range(width):
            pixels[x, y] = (red, green, blue)
    return gradient


def _render_text_mask(font: ImageFont.FreeTypeFont, text: str) -> Image.Image:
    """Render text to a tight mask that includes full glyph ink (descenders)."""
    scratch = Image.new("L", (4096, 512), 0)
    draw = ImageDraw.Draw(scratch)
    draw.text((0, 0), text, fill=255, font=font)
    bbox = scratch.getbbox()
    if bbox is None:
        raise RuntimeError(f"Could not render text mask for {text!r}")
    return scratch.crop(bbox)


def _draw_gradient_line(
    canvas: Image.Image,
    text: str,
    box: tuple[int, int, int, int],
) -> None:
    left, top, right, bottom = box
    box_height = bottom - top
    font = _fit_font(text, box)

    mask = _render_text_mask(font, text)
    text_width, text_height = mask.size

    gradient = _gradient_image(text_width, text_height).convert("RGBA")
    gradient.putalpha(mask)

    x = left
    y = top + max(0, (box_height - text_height) // 2)
    canvas.alpha_composite(gradient, (x, y))


def main() -> int:
    template = Image.open(TEMPLATE).convert("RGBA")
    for line, box in zip(LINES, LINE_BOXES, strict=True):
        _draw_gradient_line(template, line, box)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    template.convert("RGB").save(OUTPUT, format="JPEG", quality=92, optimize=True)
    print(f"saved: {OUTPUT}")
    print(f"size: {template.size[0]}x{template.size[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
