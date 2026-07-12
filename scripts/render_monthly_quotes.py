"""Render daily quote images for a month."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def main() -> int:
    _configure_stdio()

    from media_publisher.quotes_render_pipeline import QuotesRenderPipelineError, render_monthly_quotes
    from media_publisher.sources.google_drive import GoogleDriveClient
    from media_publisher.sources.google_sheets import GoogleSheetsClient
    from media_publisher.sources.quotes_config import load_quotes_sources_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    parser.add_argument(
        "--config",
        default="config/quotes_sources.json",
        help="Quotes sources config path",
    )
    parser.add_argument(
        "--variant",
        choices=("fbyt", "ig", "all"),
        default="all",
        help="Which output variant to render",
    )
    parser.add_argument(
        "--day",
        type=int,
        help="Render only one day of the month",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-render even if the output JPEG already exists",
    )
    parser.add_argument(
        "--quote-font",
        type=Path,
        help="Optional path to a TTF/OTF serif font",
    )
    args = parser.parse_args()

    config = load_quotes_sources_config(PROJECT_ROOT / args.config)
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    sheets = GoogleSheetsClient.from_service_account(sa_path)
    drive = GoogleDriveClient.from_service_account(sa_path)

    variants = ("fbyt", "ig") if args.variant == "all" else (args.variant,)
    try:
        rendered = render_monthly_quotes(
            config=config,
            sheets_client=sheets,
            drive_client=drive,
            year=args.year,
            month=args.month,
            variants=variants,
            font_path=args.quote_font,
            overwrite=args.overwrite,
            day=args.day,
        )
    except (QuotesRenderPipelineError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1

    for item in rendered:
        print(
            f"{item.variant}\t{item.stem}\tlines={item.line_count}\t"
            f"layout={item.layout_key}\t{item.image_path}"
        )

    print(f"Rendered {len(rendered)} image(s) for {args.year}-{args.month:02d}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
