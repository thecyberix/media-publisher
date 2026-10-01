"""Schedule a programme event photo to Facebook via Playwright (local).

Same inputs as ``--publish-event`` / the Publish event workflow, but posts through
Business Suite (headed Chrome) so Development-mode Graph visibility is not an
issue. History: ``downloads/events/facebook-browser-schedule-history.json``.

Prerequisites:
  python -m media_publisher --facebook-browser-login
  META_PAGE_USERNAME in .env
  Google service account + Drive Events images

Examples:
  python scripts/schedule_event_facebook.py \\
    --event-type surya_kriya --city София --date 2026-10-12 --time 10:00 \\
    --registration-link https://example.com/register --dry-run

  python scripts/schedule_event_facebook.py \\
    --event-type surya_kriya --city София --date 2026-10-12 --time 10:00 \\
    --registration-link https://example.com/register --image-number 1

  python scripts/schedule_event_facebook.py ... --now
  python scripts/schedule_event_facebook.py ... --at 2026-10-01T08:00:00
  python scripts/schedule_event_facebook.py ... --skip-page
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _default_schedule_at(settings) -> datetime:
    tz_name = settings.publish_timezone or "Europe/Sofia"
    from media_publisher.timezones import get_timezone

    tz = get_timezone(tz_name)
    local_now = datetime.now(tz)
    hour = (
        settings.quotes_publish_hour
        if settings.quotes_publish_hour is not None
        else 8
    )
    return (local_now + timedelta(days=1)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )


def main() -> int:
    _configure_stdio()

    from media_publisher.config import load_settings
    from media_publisher.events.facebook_browser_schedule import (
        EVENT_TYPE_CHOICES,
        EventFacebookScheduleError,
        default_history_path,
        history_blocks_item,
        load_schedule_history,
        prepare_event_for_facebook_schedule,
        schedule_event_via_browser,
    )
    from media_publisher.events.page import default_events_root
    from media_publisher.events.templates import normalize_event_type
    from media_publisher.publishers.facebook_web import (
        facebook_photo_via_browser_enabled,
        resolve_facebook_browser_state_path,
    )
    from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError
    from media_publisher.timezones import get_timezone

    parser = argparse.ArgumentParser(
        description=(
            "Schedule an event photo post to Facebook via Playwright; "
            "optionally update events/data/events.json."
        )
    )
    parser.add_argument(
        "--event-type",
        required=True,
        choices=EVENT_TYPE_CHOICES,
        help="Programme type",
    )
    parser.add_argument("--city", required=True, help="City (fills [град])")
    parser.add_argument(
        "--country",
        default="",
        help="Country (default: TARGET_LANGUAGE country from config)",
    )
    parser.add_argument("--date", required=True, help="Event date YYYY-MM-DD")
    parser.add_argument("--time", required=True, help="Event time HH:MM (publish TZ)")
    parser.add_argument(
        "--registration-link",
        required=True,
        help="Registration URL for the caption CTA",
    )
    parser.add_argument(
        "--image-id",
        "--image-number",
        dest="image_id",
        default="",
        metavar="SELECTOR",
        help=(
            "Facebook image: Drive file id, filename (e.g. 1.jpg), or number "
            "(e.g. 1 → 1.jpg). Blank rotates unused images in the programme folder."
        ),
    )
    parser.add_argument(
        "--events-root",
        default="",
        help="Events site root (default: events/)",
    )
    parser.add_argument(
        "--history",
        type=Path,
        help=(
            "History JSON (default: "
            "downloads/events/facebook-browser-schedule-history.json)"
        ),
    )
    parser.add_argument(
        "--at",
        metavar="ISO_DATETIME",
        help="Schedule Facebook post at this local time (default: tomorrow quotes hour)",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Publish immediately (visible on the Page now)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render + resolve image only; do not open the browser",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Schedule even if history already has this event fingerprint",
    )
    parser.add_argument(
        "--skip-page",
        action="store_true",
        help="Do not append to events/data/events.json / rebuild index.html",
    )
    args = parser.parse_args()

    if args.now and args.at:
        parser.error("Use either --now or --at, not both")

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
    publish_at = None
    if args.at:
        raw = datetime.fromisoformat(args.at)
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=get_timezone(tz_name))
        publish_at = raw
    elif not args.now:
        publish_at = _default_schedule_at(settings)

    history_path = (
        args.history if args.history is not None else default_history_path(PROJECT_ROOT)
    )
    if not history_path.is_absolute():
        history_path = PROJECT_ROOT / history_path

    events_root = (
        Path(args.events_root).expanduser()
        if args.events_root.strip()
        else default_events_root(PROJECT_ROOT)
    )
    if not events_root.is_absolute():
        events_root = PROJECT_ROOT / events_root

    service_account = PROJECT_ROOT / settings.google_sheets_service_account
    if not service_account.is_file():
        print(f"Missing Google service account credentials ({service_account})")
        return 1
    try:
        drive = GoogleDriveClient.from_service_account(service_account)
    except GoogleDriveError as exc:
        print(f"Drive setup failed: {exc}")
        return 1

    country = args.country.strip() or settings.target_country
    print(
        f"Preparing {normalize_event_type(args.event_type)} "
        f"in {args.city}, {country}…"
    )
    try:
        item = prepare_event_for_facebook_schedule(
            project_root=PROJECT_ROOT,
            drive_client=drive,
            event_type=args.event_type,
            city=args.city,
            country=country,
            date_text=args.date,
            time_text=args.time,
            registration_link=args.registration_link,
            image_id=args.image_id.strip() or None,
            publish_at=publish_at,
            language=settings.target_language or "bg",
            events_root=events_root,
        )
    except EventFacebookScheduleError as exc:
        print(f"Error: {exc}")
        return 1

    history = load_schedule_history(history_path)
    if history_blocks_item(
        history,
        dedupe_key=item.dedupe_key,
        content_fingerprint=item.content_fingerprint,
        force=args.force,
    ):
        entry = history[item.dedupe_key]
        print(
            f"Skip {item.dedupe_key}: already in history "
            f"({entry.get('permalink')}). Use --force to re-schedule."
        )
        return 0

    print(f"Image: {item.image.drive_file.name} ({item.image.selection})")
    print(f"History: {history_path}")
    try:
        permalink = schedule_event_via_browser(
            item,
            page_username=settings.meta_page_username,
            project_root=PROJECT_ROOT,
            history=history,
            history_path=history_path,
            display_timezone=tz_name,
            dry_run=args.dry_run,
            update_page=not args.skip_page,
            events_root=events_root,
            print_line=print,
        )
    except EventFacebookScheduleError as exc:
        print(f"Error: {exc}")
        return 1

    if args.dry_run:
        print("Dry-run complete.")
    else:
        print(f"Done: {permalink}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
