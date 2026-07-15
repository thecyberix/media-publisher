"""Publish already-uploaded Facebook Reels that were left as DRAFT.

Reads Facebook permalinks/IDs from Airtable (SG-FB-Published video) and/or
CLI arguments, then calls Graph API finish with video_state=PUBLISHED.

Usage:
  python scripts/publish_facebook_draft_reels.py --dry-run
  python scripts/publish_facebook_draft_reels.py --days 14
  python scripts/publish_facebook_draft_reels.py --video-id 1234567890
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _parse_airtable_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def main() -> int:
    _configure_stdio()

    from media_publisher.config import load_settings
    from media_publisher.publishers.meta import (
        MetaClient,
        MetaError,
        extract_facebook_video_id,
        normalize_facebook_page_username,
    )
    from media_publisher.sources.airtable import (
        AirtableClient,
        FIELD_SG_FB_DATE,
        FIELD_SG_FB_PUBLISHED,
        FIELD_TITLE,
        FIELD_TYPE,
        catalog_title,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--video-id",
        action="append",
        default=[],
        help="Facebook video/reel id or permalink (repeatable). Skips Airtable when set.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="When reading Airtable, only include FB posts from the last N days (default: 30).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List targets without calling the publish API.",
    )
    parser.add_argument(
        "--type",
        choices=("Reel", "Short", "Video", "all"),
        default="Reel",
        help="Airtable Type filter when scanning SG-FB-Published video (default: Reel).",
    )
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    if not settings.meta_access_token:
        print("ERROR: META_ACCESS_TOKEN is required", file=sys.stderr)
        return 1

    client = MetaClient(
        settings.meta_access_token,
        api_version=settings.meta_api_version,
        app_id=settings.meta_app_id,
    )
    page_username = normalize_facebook_page_username(settings.meta_page_username)
    page_info = client.resolve_page_by_username(page_username)
    page_id = settings.meta_page_id or page_info.page_id
    print(f"Page: {page_info.name} ({page_id})")

    targets: list[tuple[str, str]] = []  # (video_id, label)

    if args.video_id:
        for raw in args.video_id:
            video_id = extract_facebook_video_id(raw) or raw.strip()
            if not re.fullmatch(r"\d{5,}", video_id):
                print(f"SKIP: could not parse video id from {raw!r}")
                continue
            targets.append((video_id, raw.strip()))
    else:
        if not settings.airtable_token:
            print("ERROR: AIRTABLE_TOKEN is required when not using --video-id", file=sys.stderr)
            return 1
        airtable = AirtableClient(
            settings.airtable_token,
            settings.airtable_base_id,
            settings.airtable_table_name,
        )
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(args.days, 0))
        for record in airtable.list_records():
            fields = record.fields
            if args.type != "all" and str(fields.get(FIELD_TYPE) or "").strip() != args.type:
                continue
            published = fields.get(FIELD_SG_FB_PUBLISHED)
            if not isinstance(published, str) or not published.strip():
                continue
            fb_date = _parse_airtable_datetime(fields.get(FIELD_SG_FB_DATE))
            if fb_date is not None and fb_date < cutoff:
                continue
            video_id = extract_facebook_video_id(published)
            if video_id is None:
                print(
                    f"SKIP {record.id}: no Facebook video id in {published!r} "
                    f"({catalog_title(fields) or fields.get(FIELD_TITLE)!r})"
                )
                continue
            label = catalog_title(fields) or str(fields.get(FIELD_TITLE) or record.id)
            targets.append((video_id, f"{label} | {published.strip()}"))

    if not targets:
        print("No Facebook Reel targets found.")
        return 0

    print(f"Found {len(targets)} target(s).")
    failures = 0
    for video_id, label in targets:
        if args.dry_run:
            print(f"DRY-RUN would publish {video_id} ({label})")
            continue
        try:
            client.publish_existing_facebook_reel(page_id=page_id, video_id=video_id)
            permalink = client.get_facebook_video_permalink(video_id)
            print(f"OK {video_id} -> {permalink} ({label})")
        except MetaError as exc:
            failures += 1
            print(f"FAIL {video_id}: {exc} ({label})", file=sys.stderr)

    if args.dry_run:
        print("Dry run complete — no API publish calls were made.")
        return 0
    print(f"Done: {len(targets) - failures} published, {failures} failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
