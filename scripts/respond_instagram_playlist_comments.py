"""Reply and DM on Instagram comments for Inner Engineering and Miracle of Mind.

Only videos already on the watch list are checked. That list starts from
output/instagram-end-card-comments.json and grows when a publish posts the
playlist comment. A dry run lists keyword comments. Pass --apply to send.
The run stops when Instagram rate-limits the request. This does not call Airtable.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.playlists import load_playlist_config  # noqa: E402
from media_publisher.config import load_settings  # noqa: E402
from media_publisher.instagram_comment_responses import (  # noqa: E402
    DEFAULT_COMMENT_REPORT,
    DEFAULT_STATE_RELATIVE_PATH,
    run_instagram_comment_responses,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Send the reply and DM. Without this, only report.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Check at most this many videos from the watch list. 0 checks all of them.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / DEFAULT_COMMENT_REPORT,
        help="End-card comment report merged into the watch list when the file exists.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / DEFAULT_STATE_RELATIVE_PATH,
        help="File that records replies and DMs already sent.",
    )
    args = parser.parse_args()
    if args.limit < 0:
        print("--limit must be 0 or greater", file=sys.stderr)
        return 1

    settings = load_settings(ROOT)
    missing = []
    if not settings.meta_access_token:
        missing.append("META_ACCESS_TOKEN")
    if not settings.meta_page_username:
        missing.append("META_PAGE_USERNAME")
    if not settings.meta_instagram_username:
        missing.append("META_INSTAGRAM_USERNAME")
    if missing:
        print("Missing Instagram credentials: " + ", ".join(missing), file=sys.stderr)
        return 1

    from media_publisher.__main__ import meta_client_from_settings, resolve_meta_targets

    client = meta_client_from_settings(settings)
    page_id, _instagram_account_id, _page = resolve_meta_targets(settings)
    return run_instagram_comment_responses(
        client,
        page_id=page_id,
        own_username=settings.meta_instagram_username,
        config=load_playlist_config(),
        state_path=args.state,
        apply=args.apply,
        limit=args.limit,
        report_path=args.report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
