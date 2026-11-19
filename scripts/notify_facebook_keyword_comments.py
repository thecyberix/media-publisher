"""Email NOTIFY_EMAIL for new keyword comments on watched Facebook posts.

Grok Bot keeps the watch list in data/facebook_comment_responses.json. A dry
run lists matches. Pass --apply to send the mail and record that it was sent.
This does not reply or send the DM.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.playlists import load_playlist_config  # noqa: E402
from media_publisher.__main__ import meta_client_from_settings  # noqa: E402
from media_publisher.config import load_settings  # noqa: E402
from media_publisher.facebook_comment_responses import (  # noqa: E402
    DEFAULT_STATE_RELATIVE_PATH,
    run_facebook_comment_responses,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Send the email and record it. Without this, only report.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Check at most this many posts. 0 checks all of them.",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / DEFAULT_STATE_RELATIVE_PATH,
        help="Watch list of public Facebook posts and emailed comments.",
    )
    args = parser.parse_args()
    if args.limit < 0:
        print("--limit must be 0 or greater", file=sys.stderr)
        return 1

    settings = load_settings(ROOT)
    if not settings.meta_access_token:
        print("Missing META_ACCESS_TOKEN", file=sys.stderr)
        return 1
    client = meta_client_from_settings(settings)
    return run_facebook_comment_responses(
        client,
        config=load_playlist_config(),
        state_path=args.state,
        apply=args.apply,
        limit=args.limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())
