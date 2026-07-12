"""Sync Canva line-count template pages and extract layout JSON."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.__main__ import canva_client_from_settings
from media_publisher.sources.canva import CanvaError, parse_design_id


def _load_sources_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise CanvaError(f"Invalid quotes sources config: {path}")
    return payload


def _detect_panel_top_px(path: Path) -> int:
    from PIL import Image

    img = Image.open(path).convert("RGB")
    pixels = img.load()
    width, height = img.size
    for y in range(int(height * 0.35), int(height * 0.75)):
        white = 0
        for x in range(width):
            red, green, blue = pixels[x, y]
            if red > 250 and green > 250 and blue > 250:
                white += 1
        if white >= width * 0.95:
            return y
    return 674


def _analyze_template_png(
    path: Path,
    *,
    expected_lines: int,
    panel_top_px: int = 674,
    signature_top_px: int = 930,
) -> dict:
    from collections import Counter
    import statistics

    from PIL import Image

    img = Image.open(path).convert("RGB")
    width, height = img.size
    pixels = img.load()

    panel_top = panel_top_px
    signature_top = signature_top_px
    dark: list[tuple[int, int]] = []
    for y in range(panel_top + 40, signature_top):
        for x in range(width):
            red, green, blue = pixels[x, y]
            if red < 70 and green < 70 and blue < 70:
                dark.append((x, y))

    if not dark:
        raise CanvaError(f"No quote text detected in template {path.name}")

    y_counts = Counter(point[1] for point in dark)
    rows = sorted(y for y, count in y_counts.items() if count >= 20)

    lines: list[list[int]] = []
    if rows:
        current = [rows[0]]
        for y in rows[1:]:
            if y - current[-1] <= 8:
                current.append(y)
            else:
                lines.append(current)
                current = [y]
        lines.append(current)

    line_metrics = []
    for line_rows in lines:
        line_pixels = [point for point in dark if point[1] in line_rows]
        xs = [point[0] for point in line_pixels]
        ys = [point[1] for point in line_pixels]
        line_metrics.append(
            {
                "y_top": min(ys),
                "y_bottom": max(ys),
                "y_center": round(statistics.mean(ys), 1),
                "height": max(ys) - min(ys) + 1,
                "left": min(xs),
                "right": max(xs),
                "width": max(xs) - min(xs) + 1,
            }
        )

    if len(line_metrics) > expected_lines:
        quote_lines = line_metrics[:expected_lines]
    elif len(line_metrics) > 1 and line_metrics[-1]["width"] < line_metrics[0]["width"] * 0.75:
        quote_lines = line_metrics[:-1][:expected_lines]
    else:
        quote_lines = line_metrics[:expected_lines]

    quote_pixels = [
        point
        for point in dark
        if any(point[1] in line_rows for line_rows in lines[: len(quote_lines)])
    ]
    xs = [point[0] for point in quote_pixels]
    ys = [point[1] for point in quote_pixels]
    body_top = min(ys)
    body_bottom = max(ys)
    max_line_width = max(line["width"] for line in quote_lines)
    text_x = (width - max_line_width) // 2
    pad_y = 8
    text_y = body_top - pad_y
    text_height = body_bottom - body_top + 1 + 2 * pad_y

    line_heights = [line["height"] for line in quote_lines]
    line_centers = [line["y_center"] for line in quote_lines]
    spacings = [
        line_centers[index + 1] - line_centers[index]
        for index in range(len(line_centers) - 1)
    ]
    line_height = statistics.median(line_heights)
    font_size = round(line_height / 1.25 * 1.85)

    return {
        "max_lines": len(quote_lines),
        "text_box_px": {
            "x": text_x,
            "y": text_y,
            "width": max_line_width,
            "height": text_height,
        },
        "text_box_norm": {
            "x": round(text_x / width, 4),
            "y": round(text_y / height, 4),
            "width": round(max_line_width / width, 4),
            "height": round(text_height / height, 4),
        },
        "line_centers_px": line_centers,
        "font_size_px": font_size,
        "line_height_px": round(line_height, 1),
        "line_spacing_px": round(statistics.median(spacings), 1) if spacings else round(line_height, 1),
    }


def _scale_layout_from_fbyt(
    *,
    fbyt_layout: dict,
    fbyt_canvas_height: int,
    ig_canvas_height: int,
    panel_top_px: int,
    expected_lines: int,
) -> dict:
    fbyt_panel_height = fbyt_canvas_height - panel_top_px
    ig_panel_height = ig_canvas_height - panel_top_px
    scale = ig_panel_height / fbyt_panel_height

    fbyt_box = fbyt_layout["text_box_px"]
    ig_width = min(int(fbyt_box["width"] * scale), 1080 - 40)
    ig_x = (1080 - ig_width) // 2

    fbyt_centers = fbyt_layout.get("line_centers_px", [])
    ig_centers = [
        panel_top_px + (center - panel_top_px) * scale for center in fbyt_centers
    ]
    if len(ig_centers) < expected_lines:
        start = panel_top_px + 60
        end = panel_top_px + ig_panel_height - 120
        step = (end - start) / (expected_lines + 1)
        ig_centers = [start + step * (index + 1) for index in range(expected_lines)]

    top = min(ig_centers) - fbyt_layout.get("line_height_px", 30)
    bottom = max(ig_centers) + fbyt_layout.get("line_height_px", 30)
    return {
        "max_lines": expected_lines,
        "text_box_px": {
            "x": ig_x,
            "y": int(top),
            "width": ig_width,
            "height": int(bottom - top),
        },
        "line_centers_px": [round(value, 1) for value in ig_centers[:expected_lines]],
        "font_size_px": int(round(fbyt_layout.get("font_size_px", 22) * scale * 0.95)),
        "line_height_px": round(float(fbyt_layout.get("line_height_px", 27)) * scale, 1),
        "line_spacing_px": round(float(fbyt_layout.get("line_spacing_px", 50)) * scale, 1),
    }


def sync_variant(
    *,
    variant: str,
    template_config: dict,
    client,
    download_dir: Path,
) -> Path:
    design_id = parse_design_id(template_config["design_id"])
    local_dir = PROJECT_ROOT / template_config["local_dir"]
    local_dir.mkdir(parents=True, exist_ok=True)

    layout_pages = template_config.get("layout_pages", {})
    for line_key, page_number in layout_pages.items():
        destination = local_dir / f"{line_key}.png"
        client.download_design_image(
            design_id,
            destination,
            export_format="png",
            pages=[int(page_number)],
        )
        print(f"  saved {destination.relative_to(PROJECT_ROOT)}")

    from PIL import Image

    sample = Image.open(local_dir / "1.png")
    width, height = sample.size
    panel_top_px = _detect_panel_top_px(local_dir / "4.png")
    signature_top_px = 1240 if variant == "ig" else 930

    layouts: dict[str, dict] = {}
    fbyt_layouts_path = PROJECT_ROOT / "config" / "quote_layouts_fbyt.json"
    fbyt_layouts_payload = None
    if variant == "ig" and fbyt_layouts_path.is_file():
        fbyt_layouts_payload = json.loads(fbyt_layouts_path.read_text(encoding="utf-8"))

    for line_key in sorted(layout_pages, key=int):
        layout = _analyze_template_png(
            local_dir / f"{line_key}.png",
            expected_lines=int(line_key),
            panel_top_px=panel_top_px,
            signature_top_px=signature_top_px,
        )
        if (
            variant == "ig"
            and fbyt_layouts_payload is not None
            and layout["max_lines"] < int(line_key)
        ):
            fbyt_layout = fbyt_layouts_payload.get("layouts", {}).get(line_key)
            if isinstance(fbyt_layout, dict):
                layout = _scale_layout_from_fbyt(
                    fbyt_layout=fbyt_layout,
                    fbyt_canvas_height=int(
                        fbyt_layouts_payload.get("canvas", {}).get("height", 1080)
                    ),
                    ig_canvas_height=height,
                    panel_top_px=int(
                        fbyt_layouts_payload.get("structure", {}).get(
                            "white_panel_top_px", 674
                        )
                    ),
                    expected_lines=int(line_key),
                )
        box = layout["text_box_px"]
        layouts[line_key] = {
            "max_lines": int(line_key),
            "text_box_px": box,
            "text_box_norm": {
                "x": round(box["x"] / width, 4),
                "y": round(box["y"] / height, 4),
                "width": round(box["width"] / width, 4),
                "height": round(box["height"] / height, 4),
            },
            "line_centers_px": layout["line_centers_px"],
            "font_size_px": layout["font_size_px"],
            "line_height_px": layout["line_height_px"],
            "line_spacing_px": layout["line_spacing_px"],
        }

    layouts_config_path = PROJECT_ROOT / template_config["layouts_config"]
    payload = {
        "variant": variant,
        "description": f"{template_config.get('title', variant)} ({width}x{height})",
        "source_templates": template_config["local_dir"],
        "canva_design_id": design_id,
        "canvas": {"width": width, "height": height},
        "structure": {
            "photo_area_ratio": round(panel_top_px / height, 4),
            "white_panel_top_px": panel_top_px,
            "quote_icon_center_y_px": panel_top_px,
            "quote_icon": {
                "center_x_px": width // 2,
                "center_y_px": panel_top_px,
                "radius_px": 52,
            },
            "signature_top_px": signature_top_px,
        },
        "typography": {
            "font_family": "Times New Roman",
            "font_weight": "normal",
            "font_size_px": layouts["1"].get("font_size_px", 23),
            "line_height_px": layouts["1"].get("line_height_px", 27),
            "line_spacing_px": layouts["1"].get("line_spacing_px", 50),
            "color": "#000000",
            "align": "center",
        },
        "layouts": layouts,
        "layout_selection": {
            "rule": "Choose layout by wrapped line count (1-4).",
            "wrap_width_px": max(
                layout["text_box_px"]["width"] for layout in layouts.values()
            ),
        },
    }
    layouts_config_path.parent.mkdir(parents=True, exist_ok=True)
    layouts_config_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"  wrote {layouts_config_path.relative_to(PROJECT_ROOT)}")
    return layouts_config_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config/quotes_sources.json",
        help="Quotes sources config path (default: config/quotes_sources.json)",
    )
    parser.add_argument(
        "--variant",
        choices=("fbyt", "ig", "all"),
        default="all",
        help="Which Canva template variant to sync (default: all)",
    )
    args = parser.parse_args()

    config_path = PROJECT_ROOT / args.config
    sources = _load_sources_config(config_path)
    templates = sources.get("canva_templates", {})
    if not isinstance(templates, dict):
        raise CanvaError("quotes_sources.json is missing canva_templates")

    settings = load_settings()
    client = canva_client_from_settings(settings)

    variants = ["fbyt", "ig"] if args.variant == "all" else [args.variant]
    for variant in variants:
        template_config = templates.get(variant)
        if not isinstance(template_config, dict):
            print(f"Skipping unknown variant {variant!r}")
            continue
        print(f"Syncing Canva templates for {variant}...")
        sync_variant(
            variant=variant,
            template_config=template_config,
            client=client,
            download_dir=PROJECT_ROOT / settings.canva_download_dir,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CanvaError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)
