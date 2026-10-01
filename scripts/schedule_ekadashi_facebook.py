"""Schedule Ekadashi photo posts to Facebook + Instagram via Playwright (local).

Exports pages from Canva folder ``FAHWrBd1fUE`` / design ``Ekadashi {year}``,
pairs each with the hard-coded 2025 caption for that slot, and schedules through
Business Suite with Instagram left selected.

Prerequisites:
  python -m media_publisher --facebook-browser-login
  Valid Canva token (``python -m media_publisher --canva-auth`` …)
  META_PAGE_USERNAME in .env

Examples:
  python scripts/schedule_ekadashi_facebook.py --year 2026 --month 10 --dry-run
  python scripts/schedule_ekadashi_facebook.py --year 2026 --month 10
  python scripts/schedule_ekadashi_facebook.py --year 2026 --month 10 --day 6
  python scripts/schedule_ekadashi_facebook.py --year 2026 --month 10 --force

Dates are read from the public Isha Google Calendar ICS
(ishacalendar@gmail.com). Each post is scheduled for Facebook + Instagram
one day before the Ekadashi date at 12:30 Europe/Sofia.
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
    from media_publisher.ekadashi import CANVA_EKADASHI_FOLDER_ID
    from media_publisher.ekadashi_facebook_browser_schedule import (
        DEFAULT_SCHEDULE_HOUR,
        DEFAULT_SCHEDULE_MINUTE,
        EkadashiFacebookScheduleError,
        default_history_path,
        discover_ekadashi_for_month,
        load_schedule_history,
        schedule_ekadashi_via_browser,
        select_pending_schedule_items,
    )
    from media_publisher.publishers.facebook_web import (
        facebook_photo_via_browser_enabled,
        resolve_facebook_browser_state_path,
    )
    from media_publisher.sources.canva import CanvaClient, CanvaError
    from media_publisher.timezones import get_timezone

    parser = argparse.ArgumentParser(
        description=(
            "Schedule Ekadashi Canva pages to Facebook + Instagram via Playwright; "
            "skip days already recorded in local history."
        )
    )
    parser.add_argument("--year", type=int, required=True, help="Calendar year")
    parser.add_argument("--month", type=int, required=True, help="Calendar month (1-12)")
    parser.add_argument("--day", type=int, help="Only this day of the month")
    parser.add_argument(
        "--folder-id",
        default=CANVA_EKADASHI_FOLDER_ID,
        help=f"Canva folder id (default: {CANVA_EKADASHI_FOLDER_ID})",
    )
    parser.add_argument(
        "--ics",
        type=Path,
        help="Optional local ICS file instead of downloading the Isha calendar",
    )
    parser.add_argument(
        "--history",
        type=Path,
        help=(
            "History JSON path (default: "
            "downloads/ekadashi/facebook-browser-schedule-history.json)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Export/list pending schedules without opening the browser",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-schedule even when history already has the same fingerprint",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=DEFAULT_SCHEDULE_HOUR,
        help=f"Local schedule hour (default: {DEFAULT_SCHEDULE_HOUR})",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=DEFAULT_SCHEDULE_MINUTE,
        help=f"Local schedule minute (default: {DEFAULT_SCHEDULE_MINUTE})",
    )
    args = parser.parse_args()

    if args.month < 1 or args.month > 12:
        parser.error("--month must be 1-12")
    if args.day is not None and (args.day < 1 or args.day > 31):
        parser.error("--day must be 1-31")
    if args.hour < 0 or args.hour > 23:
        parser.error("--hour must be 0-23")
    if args.minute < 0 or args.minute > 59:
        parser.error("--minute must be 0-59")

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
    if not settings.canva_client_id or not settings.canva_client_secret:
        print("CANVA_CLIENT_ID and CANVA_CLIENT_SECRET are required in .env")
        return 1

    tz_name = settings.publish_timezone or "Europe/Sofia"
    hour = args.hour
    minute = args.minute
    history_path = (
        args.history
        if args.history is not None
        else default_history_path(PROJECT_ROOT)
    )
    if not history_path.is_absolute():
        history_path = PROJECT_ROOT / history_path

    ics_path = args.ics
    if ics_path is not None and not ics_path.is_absolute():
        ics_path = PROJECT_ROOT / ics_path

    try:
        canva = CanvaClient(
            client_id=settings.canva_client_id or "",
            client_secret=settings.canva_client_secret or "",
            token_path=PROJECT_ROOT / settings.canva_token,
            api_base=settings.canva_api_base,
            redirect_uri=settings.canva_redirect_uri,
        )
    except CanvaError as exc:
        print(f"Canva setup failed: {exc}")
        return 1

    print(f"Discovering Ekadashi for {args.year:04d}-{args.month:02d}…")
    try:
        items, warnings = discover_ekadashi_for_month(
            canva_client=canva,
            project_root=PROJECT_ROOT,
            year=args.year,
            month=args.month,
            publish_timezone=tz_name,
            publish_hour=hour,
            publish_minute=minute,
            day=args.day,
            folder_id=args.folder_id.strip() or CANVA_EKADASHI_FOLDER_ID,
            ics_path=ics_path,
        )
    except EkadashiFacebookScheduleError as exc:
        print(f"Error: {exc}")
        return 1

    for warning in warnings:
        print(f"Note: {warning}")

    history = load_schedule_history(history_path)
    now = datetime.now(get_timezone(tz_name))
    pending, skips = select_pending_schedule_items(
        items, history, now=now, force=args.force
    )
    for skip in skips:
        print(f"Skip {skip.stem}: {skip.reason}")

    if not pending:
        print("Nothing to schedule.")
        return 0

    print(f"History: {history_path}")
    print(f"Scheduling {len(pending)} post(s) to Facebook + Instagram…")
    ok, failed, errors = schedule_ekadashi_via_browser(
        pending,
        page_username=settings.meta_page_username,
        project_root=PROJECT_ROOT,
        history=history,
        history_path=history_path,
        display_timezone=tz_name,
        dry_run=args.dry_run,
        print_line=print,
    )
    for message in errors:
        print(f"Error: {message}")
    print(f"Done: {ok} ok, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
