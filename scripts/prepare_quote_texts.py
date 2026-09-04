"""Prepare Bulgarian quote texts from the English source sheet (current + next month)."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
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

    from media_publisher.config import load_settings
    from media_publisher.quotes_text_sync import sync_quote_texts_for_months
    from media_publisher.sources.google_drive import GoogleDriveClient
    from media_publisher.sources.google_sheets import GoogleSheetsClient
    from media_publisher.sources.quotes_config import load_quotes_sources_config
    from media_publisher.timezones import get_timezone

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config/quotes_sources.json",
        help="Quotes sources config path",
    )
    parser.add_argument("--year", type=int, help="Override year (with --month)")
    parser.add_argument("--month", type=int, help="Override month (with --year)")
    parser.add_argument(
        "--timezone",
        default="Europe/Sofia",
        help="Timezone used to pick current/next month",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    config = load_quotes_sources_config(PROJECT_ROOT / args.config)
    sa_path = PROJECT_ROOT / settings.google_sheets_service_account
    sheets = GoogleSheetsClient.from_service_account(sa_path)
    drive = GoogleDriveClient.from_service_account(sa_path)

    today = datetime.now(get_timezone(args.timezone)).date()
    if args.year is not None and args.month is not None:
        reference = date(args.year, args.month, min(today.day, 28))
        months = [(args.year, args.month)]
    else:
        reference = today
        months = None

    result = sync_quote_texts_for_months(
        config=config,
        sheets=sheets,
        drive=drive,
        reference_date=reference,
        project_root=PROJECT_ROOT,
        print_line=print,
        months=months,
    )
    for warning in result.warnings:
        print(f"Warning: {warning}")
    print(
        f"Done: {result.added_count} added, {result.updated_count} updated, "
        f"{result.reused_count} reused, {result.translated_count} AI-translated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
