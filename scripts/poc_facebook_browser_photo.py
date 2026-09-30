"""PoC: schedule a Facebook Page photo via Playwright (not live on the feed).

Usage:
  python -m media_publisher --facebook-browser-login
  python scripts/poc_facebook_browser_photo.py path/to/image.jpg "Caption text"

By default the post is scheduled for tomorrow at the quotes hour (or 10:00) in
PUBLISH_JSON timezone so it does not appear on the public Page feed. Pass
--now to publish immediately instead.

META_PAGE_USERNAME must be set in .env. Browser photo publishing is used when a
Facebook session file exists (or FACEBOOK_BROWSER_STATE_JSON is set); this script
always uses the browser path regardless.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.publishers.facebook_web import (
    FacebookWebError,
    publish_facebook_photo_via_browser,
    resolve_facebook_browser_channel,
    resolve_facebook_browser_profile_dir,
    resolve_facebook_browser_state_path,
)
from media_publisher.timezones import get_timezone


def _default_schedule_at(settings) -> datetime:
    tz_name = settings.publish_timezone or "Europe/Sofia"
    tz = get_timezone(tz_name)
    local_now = datetime.now(tz)
    hour = settings.quotes_publish_hour if settings.quotes_publish_hour is not None else 10
    candidate = (local_now + timedelta(days=1)).replace(
        hour=hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    # Facebook requires schedule at least ~10 minutes ahead; tomorrow is fine.
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Schedule (default) or publish a Facebook Page photo via Playwright."
    )
    parser.add_argument("image", type=Path, help="Path to the image file")
    parser.add_argument("caption", nargs="+", help="Post caption text")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Publish immediately (visible on the Page). Default: schedule for tomorrow.",
    )
    parser.add_argument(
        "--at",
        metavar="ISO_DATETIME",
        help="Schedule at this local time (ISO). Example: 2026-10-01T10:00:00",
    )
    args = parser.parse_args()

    image_path = args.image
    caption = " ".join(args.caption).strip()
    settings = load_settings(ROOT)
    if not settings.meta_page_username:
        print("META_PAGE_USERNAME is required in .env")
        return 1
    if not image_path.is_file():
        print(f"Image not found: {image_path}")
        return 1

    tz_name = settings.publish_timezone or "Europe/Sofia"
    publish_at = None
    if args.now and args.at:
        print("Use either --now or --at, not both.")
        return 2
    if args.at:
        raw = datetime.fromisoformat(args.at)
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=get_timezone(tz_name))
        publish_at = raw
    elif not args.now:
        publish_at = _default_schedule_at(settings)

    screenshot = ROOT / "downloads" / "facebook-browser-poc-failure.png"
    try:
        post_id, permalink = publish_facebook_photo_via_browser(
            page_username=settings.meta_page_username,
            image_path=image_path,
            caption=caption,
            publish_at=publish_at,
            display_timezone=tz_name,
            project_root=ROOT,
            storage_state_path=resolve_facebook_browser_state_path(project_root=ROOT),
            browser_profile_dir=resolve_facebook_browser_profile_dir(project_root=ROOT),
            browser_channel=resolve_facebook_browser_channel(),
            headless=False,
            failure_screenshot=screenshot,
        )
    except FacebookWebError as exc:
        print(f"PoC failed: {exc}")
        if screenshot.is_file():
            print(f"Failure screenshot: {screenshot}")
        return 1

    print(f"Posted as Page {settings.meta_page_username!r}")
    if publish_at is not None:
        print(f"Scheduled for: {publish_at.isoformat()}")
        print("(Not on the public feed until that time — check Publishing tools.)")
    else:
        print("Published immediately.")
    print(f"Post id/url: {post_id}")
    print(f"Permalink: {permalink}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
