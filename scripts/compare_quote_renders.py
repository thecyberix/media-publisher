"""Compare rendered quote images against Canva reference exports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.google_sheets import GoogleSheetsClient
from media_publisher.sources.quote_layouts import load_quote_layout_config
from media_publisher.sources.quote_renderer import load_font, wrap_quote_text
from media_publisher.sources.quotes_config import load_quotes_sources_config
from media_publisher.sources.quotes_sheet import load_monthly_quote_texts


def _count_text_lines(path: Path, *, panel_top: int, signature_top: int) -> int:
    from collections import Counter

    from PIL import Image

    img = Image.open(path).convert("RGB")
    pixels = img.load()
    dark: list[tuple[int, int]] = []
    for y in range(panel_top + 40, signature_top - 40):
        for x in range(img.width):
            red, green, blue = pixels[x, y]
            if red < 70 and green < 70 and blue < 70:
                dark.append((x, y))
    if not dark:
        return 0

    y_counts = Counter(point[1] for point in dark)
    rows = sorted(y for y, count in y_counts.items() if count >= 15)
    if not rows:
        return 0

    groups: list[list[int]] = []
    current = [rows[0]]
    for y in rows[1:]:
        if y - current[-1] <= 10:
            current.append(y)
        else:
            groups.append(current)
            current = [y]
    groups.append(current)

    quote_groups = [
        group
        for group in groups
        if max(point[0] for point in dark if point[1] in group)
        - min(point[0] for point in dark if point[1] in group)
        + 1
        > 120
    ]
    return len(quote_groups)


def _render_plan_lines(
    text: str,
    *,
    variant: str,
    config_path: Path,
    template_dir: Path,
) -> tuple[int, int, tuple[str, ...]]:
    layout_config = load_quote_layout_config(config_path, template_dir=template_dir)
    from media_publisher.sources.quote_renderer import select_render_plan

    plan = select_render_plan(layout_config, text)
    return int(plan.layout_key), plan.font_size_px, plan.lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, default=2026)
    parser.add_argument("--month", type=int, default=7)
    parser.add_argument("--start-day", type=int, default=1)
    parser.add_argument("--end-day", type=int, default=15)
    parser.add_argument(
        "--fbyt-design-id", default="DAHOD9PSfLA", help="Canva FB/YT reference design id"
    )
    parser.add_argument(
        "--ig-design-id", default="DAHODx-ESo8", help="Canva IG reference design id"
    )
    args = parser.parse_args()

    config = load_quotes_sources_config(PROJECT_ROOT / "config/quotes_sources.json")
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    sheets = GoogleSheetsClient.from_service_account(sa_path)
    quotes = {
        quote.day: quote
        for quote in load_monthly_quote_texts(
            sheets,
            config,
            year=args.year,
            month=args.month,
        )
    }

    fbyt_layout = load_quote_layout_config(
        PROJECT_ROOT / "config/quote_layouts_fbyt.json",
        template_dir=PROJECT_ROOT / "config/quote_templates/fbyt",
    )
    ig_layout = load_quote_layout_config(
        PROJECT_ROOT / "config/quote_layouts_ig.json",
        template_dir=PROJECT_ROOT / "config/quote_templates/ig",
    )

    canva_dir = PROJECT_ROOT / load_settings().canva_download_dir
    results: list[dict[str, object]] = []

    for day in range(args.start_day, args.end_day + 1):
        quote = quotes.get(day)
        if quote is None:
            results.append({"day": day, "status": "missing_quote"})
            continue

        for variant, design_id, layout in (
            ("fbyt", args.fbyt_design_id, fbyt_layout),
            ("ig", args.ig_design_id, ig_layout),
        ):
            render_path = (
                PROJECT_ROOT
                / config.renders["fbyt_dir" if variant == "fbyt" else "ig_dir"]
                / f"{args.year:04d}-{args.month:02d}-{day:02d}.jpg"
            )
            ref_path = canva_dir / f"{design_id}_page{day}.png"
            if not render_path.is_file():
                results.append(
                    {
                        "day": day,
                        "variant": variant,
                        "status": "missing_render",
                        "render": str(render_path),
                    }
                )
                continue
            if not ref_path.is_file():
                results.append(
                    {
                        "day": day,
                        "variant": variant,
                        "status": "missing_reference",
                        "reference": str(ref_path),
                    }
                )
                continue

            ref_lines = _count_text_lines(
                ref_path,
                panel_top=layout.panel_top_px,
                signature_top=layout.signature_top_px,
            )
            render_lines = _count_text_lines(
                render_path,
                panel_top=layout.panel_top_px,
                signature_top=layout.signature_top_px,
            )
            layout_key, font_size, wrapped = _render_plan_lines(
                quote.text_bg,
                variant=variant,
                config_path=PROJECT_ROOT
                / (
                    "config/quote_layouts_fbyt.json"
                    if variant == "fbyt"
                    else "config/quote_layouts_ig.json"
                ),
                template_dir=PROJECT_ROOT
                / (
                    "config/quote_templates/fbyt"
                    if variant == "fbyt"
                    else "config/quote_templates/ig"
                ),
            )

            issues: list[str] = []
            if ref_lines and render_lines != ref_lines:
                issues.append(f"line_count ref={ref_lines} render={render_lines}")
            if len(wrapped) != render_lines:
                issues.append(
                    f"detected_lines={render_lines} wrapped_lines={len(wrapped)}"
                )

            from PIL import Image

            ref_img = Image.open(ref_path)
            ren_img = Image.open(render_path)
            if ref_img.size != ren_img.size:
                issues.append(f"size ref={ref_img.size} render={ren_img.size}")

            results.append(
                {
                    "day": day,
                    "variant": variant,
                    "status": "ok" if not issues else "mismatch",
                    "ref_lines": ref_lines,
                    "render_lines": render_lines,
                    "layout": layout_key,
                    "font_px": font_size,
                    "wrapped_lines": len(wrapped),
                    "issues": issues,
                    "render": str(render_path.relative_to(PROJECT_ROOT)),
                    "reference": str(ref_path.relative_to(PROJECT_ROOT)),
                }
            )

    print(json.dumps(results, indent=2, ensure_ascii=False))

    mismatches = [item for item in results if item.get("status") != "ok"]
    print(f"\nCompared {len(results)} render/reference pairs.")
    print(f"Mismatches or missing: {len(mismatches)}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
