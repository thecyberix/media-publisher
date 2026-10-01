"""Schedule prepared Drive quote JPEGs to Facebook via Playwright (local).

Downloads images from Drive ``Quotes/{MM Mon YYYY}/``, pairs them with Ready
sheet captions, and schedules each *new* day on the Page composer. History is
kept in ``downloads/quotes/facebook-browser-schedule-history.json`` so re-runs
skip days already uploaded (same image+caption fingerprint).

Prerequisites:
  python -m media_publisher --facebook-browser-login
  META_PAGE_USERNAME in .env
  Prepared month on Drive (``scripts/render_monthly_quotes.py --sync-drive``)

Examples:
  python scripts/schedule_prepared_quotes_facebook.py --year 2026 --month 10 --dry-run
  python scripts/schedule_prepared_quotes_facebook.py --year 2026 --month 10
  python scripts/schedule_prepared_quotes_facebook.py --year 2026 --month 10 --day 5
  python scripts/schedule_prepared_quotes_facebook.py --year 2026 --month 10 --force
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
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
    from media_publisher.quotes_facebook_browser_schedule import (
        QuotesFacebookScheduleError,
        default_history_path,
        discover_prepared_quotes_for_month,
        load_schedule_history,
        schedule_prepared_quotes_via_browser,
        select_pending_schedule_items,
    )
    from media_publisher.publishers.facebook_web import (
        facebook_photo_via_browser_enabled,
        resolve_facebook_browser_state_path,
    )
    from media_publisher.sources.google_drive import GoogleDriveClient
    from media_publisher.sources.google_sheets import GoogleSheetsClient
    from media_publisher.sources.quotes_config import load_quotes_sources_config
    from media_publisher.timezones import get_timezone

    parser = argparse.ArgumentParser(
        description=(
            "Schedule prepared Drive quote photos to Facebook via Playwright; "
            "skip days already recorded in local history."
        )
    )
    parser.add_argument("--year", type=int, required=True, help="Calendar year")
    parser.add_argument("--month", type=int, required=True, help="Calendar month (1-12)")
    parser.add_argument("--day", type=int, help="Only this day of the month")
    parser.add_argument(
        "--config",
        default="config/quotes_sources.json",
        help="Quotes sources config path",
    )
    parser.add_argument(
        "--history",
        type=Path,
        help=(
            "History JSON path (default: "
            "downloads/quotes/facebook-browser-schedule-history.json)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List pending schedules without opening the browser",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-schedule even when history already has the same fingerprint",
    )
    parser.add_argument(
        "--hour",
        type=int,
        help="Local publish hour (default: quotes hour from PUBLISH_JSON / settings)",
    )
    args = parser.parse_args()

    if args.month < 1 or args.month > 12:
        parser.error("--month must be 1-12")
    if args.day is not None and (args.day < 1 or args.day > 31):
        parser.error("--day must be 1-31")

    settings = load_settings(PROJECT_ROOT)
    if not settings.meta_page_username:
        print("META_PAGE_USERNAME is required in .env")
        return 1
    if not args.dry_run and not facebook_photo_via_browser_enabled(
        project_root=PROJECT_ROOT
    ):
        state = resolve_facebook_browser_state_path(project_root=PROJECT_ROOT)
        print(
            f"Facebook browser session not found ({state}). "
            "Run: python -m media_publisher --facebook-browser-login"
        )
        return 1

    tz_name = settings.publish_timezone or "Europe/Sofia"
    hour = (
        args.hour
        if args.hour is not None
        else (
            settings.quotes_publish_hour
            if settings.quotes_publish_hour is not None
            else 8
        )
    )
    history_path = (
        args.history
        if args.history is not None
        else default_history_path(PROJECT_ROOT)
    )
    if not history_path.is_absolute():
        history_path = PROJECT_ROOT / history_path

    config = load_quotes_sources_config(PROJECT_ROOT / args.config)
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    sheets = GoogleSheetsClient.from_service_account(sa_path)
    drive = GoogleDriveClient.from_service_account(sa_path)

    print(f"Discovering prepared quotes for {args.year:04d}-{args.month:02d}…")
    try:
        items, warnings = discover_prepared_quotes_for_month(
            config=config,
            sheets_client=sheets,
            drive_client=drive,
            project_root=PROJECT_ROOT,
            year=args.year,
            month=args.month,
            publish_timezone=tz_name,
            publish_hour=hour,
            day=args.day,
        )
    except QuotesFacebookScheduleError as exc:
        print(f"Error: {exc}")
        return 1

    for warning in warnings:
        print(f"Warning: {warning}")

    history = load_schedule_history(history_path)
    now = datetime.now(get_timezone(tz_name))
    pending, skips = select_pending_schedule_items(
        items,
        history,
        now=now,
        force=args.force,
    )
    for skip in skips:
        print(f"Skip {skip.stem}: {skip.reason}")

    print(
        f"Found {len(items)} prepared day(s); "
        f"{len(pending)} to schedule; history={history_path}"
    )
    if not pending:
        print("Nothing to schedule.")
        return 0

    ok, failed, errors = schedule_prepared_quotes_via_browser(
        pending,
        page_username=settings.meta_page_username,
        project_root=PROJECT_ROOT,
        history=history,
        history_path=history_path,
        display_timezone=tz_name,
        dry_run=args.dry_run,
        print_line=print,
    )
    print(
        f"Done: {ok} scheduled"
        + (" (dry-run)" if args.dry_run else "")
        + (f", {failed} failed" if failed else "")
        + f" across {len(pending)} pending day(s)."
    )
    for message in errors:
        print(f"Error: {message}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
