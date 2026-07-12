from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from media_publisher.sources.quotes_config import QuotesConfigError


class QuoteLayoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class QuoteLayoutEntry:
    key: str
    max_lines: int
    text_box: dict[str, int]
    line_centers_px: list[float]
    font_size_px: int
    line_height_px: float
    line_spacing_px: float


@dataclass(frozen=True)
class QuoteLayoutConfig:
    variant: str
    canvas_width: int
    canvas_height: int
    panel_top_px: int
    signature_top_px: int
    brand_watermark: dict[str, object] | None
    quote_icon: dict[str, object] | None
    typography: dict[str, object]
    layouts: dict[str, QuoteLayoutEntry]
    wrap_width_px: int
    template_dir: Path

    def template_path(self, layout_key: str) -> Path:
        return self.template_dir / f"{layout_key}.png"


def load_quote_layout_config(path: Path, *, template_dir: Path) -> QuoteLayoutConfig:
    if not path.is_file():
        raise QuoteLayoutError(f"Quote layout config not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QuoteLayoutError(f"Invalid quote layout config: {path}")

    canvas = payload.get("canvas")
    structure = payload.get("structure")
    layouts_raw = payload.get("layouts")
    typography = payload.get("typography")
    selection = payload.get("layout_selection")
    if not isinstance(canvas, dict) or not isinstance(structure, dict):
        raise QuoteLayoutError(f"Quote layout config missing canvas/structure: {path}")
    if not isinstance(layouts_raw, dict) or not isinstance(typography, dict):
        raise QuoteLayoutError(f"Quote layout config missing layouts/typography: {path}")

    layouts: dict[str, QuoteLayoutEntry] = {}
    for key, layout in layouts_raw.items():
        if not isinstance(layout, dict):
            continue
        text_box = layout.get("text_box_px")
        line_centers = layout.get("line_centers_px")
        if not isinstance(text_box, dict) or not isinstance(line_centers, list):
            raise QuoteLayoutError(
                f"Layout {key!r} in {path.name} is missing text_box_px or line_centers_px"
            )
        layouts[str(key)] = QuoteLayoutEntry(
            key=str(key),
            max_lines=int(key),
            text_box={
                "x": int(text_box["x"]),
                "y": int(text_box["y"]),
                "width": int(text_box["width"]),
                "height": int(text_box["height"]),
            },
            line_centers_px=[float(value) for value in line_centers],
            font_size_px=int(
                layout.get("font_size_px", typography.get("font_size_px", 22))
            ),
            line_height_px=float(
                layout.get("line_height_px", typography.get("line_height_px", 27))
            ),
            line_spacing_px=float(
                layout.get("line_spacing_px", typography.get("line_spacing_px", 50))
            ),
        )

    wrap_width_px = int(
        selection.get("wrap_width_px")
        if isinstance(selection, dict) and selection.get("wrap_width_px") is not None
        else max(entry.text_box["width"] for entry in layouts.values())
    )

    brand_watermark = structure.get("brand_watermark")
    if brand_watermark is not None and not isinstance(brand_watermark, dict):
        brand_watermark = None

    quote_icon = structure.get("quote_icon")
    if quote_icon is not None and not isinstance(quote_icon, dict):
        quote_icon = None
    elif quote_icon is None:
        center_y = structure.get("quote_icon_center_y_px")
        if center_y is not None:
            quote_icon = {"center_y_px": center_y}

    return QuoteLayoutConfig(
        variant=str(payload.get("variant", path.stem)),
        canvas_width=int(canvas["width"]),
        canvas_height=int(canvas["height"]),
        panel_top_px=int(structure.get("white_panel_top_px", 674)),
        signature_top_px=int(structure.get("signature_top_px", 930)),
        brand_watermark=brand_watermark,
        quote_icon=quote_icon,
        typography=typography,
        layouts=layouts,
        wrap_width_px=wrap_width_px,
        template_dir=template_dir,
    )
