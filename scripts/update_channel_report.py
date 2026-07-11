"""Update monthly channel views in the Bulgarian Google Sheets report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.__main__ import (  # noqa: E402
    PROJECT_ROOT as APP_ROOT,
    parse_channel_report_target_month,
    run_update_channel_report,
)
from media_publisher.config import load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch monthly YouTube/Instagram views and write them into the "
            "Bulgarian channel report tab."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show updates without writing to Google Sheets.",
    )
    parser.add_argument(
        "--all-months",
        action="store_true",
        help="Backfill every month through the last complete calendar month.",
    )
    parser.add_argument(
        "--month",
        metavar="YYYY-MM",
        help="Update one month only, e.g. 2026-02.",
    )
    args = parser.parse_args()

    settings = load_settings(APP_ROOT)
    try:
        target_month = parse_channel_report_target_month(args.month)
    except Exception as exc:
        print(f"Channel report update failed: {exc}")
        return 1

    return run_update_channel_report(
        settings,
        dry_run=args.dry_run,
        all_months=args.all_months,
        target_month=target_month,
    )


if __name__ == "__main__":
    raise SystemExit(main())
