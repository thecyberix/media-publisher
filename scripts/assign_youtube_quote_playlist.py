"""Add published quote videos to the Цитати playlist.

A quote is recognized from the title and description used at publish time.
Talk shorts and long videos are left out. A dry run only reports the matches.
Pass --apply to insert videos that are not already on the playlist.
The run stops when YouTube's daily quota is exhausted.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.__main__ import load_env_file  # noqa: E402
from media_publisher.config import load_settings  # noqa: E402
from media_publisher.post_templates import is_published_youtube_quote  # noqa: E402
from media_publisher.publishers.youtube import (  # noqa: E402
    YouTubeClient,
    YouTubePublishError,
)
from scripts.assign_youtube_end_card_playlists import (  # noqa: E402
    _quota_exceeded,
    channel_uploads_playlist_id,
    list_owned_playlists,
    list_uploads,
    playlist_video_ids,
    published_video_ids,
)

DEFAULT_PLAYLIST_TITLE = "Цитати"


def playlist_id_for_title(playlists: list[tuple[str, str]], title: str) -> str:
    for playlist_id, playlist_title in playlists:
        if playlist_title == title:
            return playlist_id
    known = ", ".join(sorted(item[1] for item in playlists)) or "(none)"
    raise YouTubePublishError(
        f"YouTube playlist title not found for {title}. Channel playlists: {known}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert quotes into the playlist. Without this, only report.",
    )
    parser.add_argument(
        "--playlist-title",
        default=DEFAULT_PLAYLIST_TITLE,
        help="Exact YouTube playlist title.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Add at most this many quotes.")
    args = parser.parse_args()

    load_env_file(ROOT / ".env")
    settings = load_settings(ROOT)
    secrets = ROOT / settings.youtube_client_secrets
    token = ROOT / settings.youtube_token
    missing = [str(path) for path in (secrets, token) if not path.is_file()]
    if missing:
        print("Missing YouTube credentials: " + ", ".join(missing), file=sys.stderr)
        return 1

    client = YouTubeClient(
        client_secrets_path=secrets,
        token_path=token,
        expected_channel_handle=settings.youtube_channel_handle or None,
    )
    try:
        playlist_id = playlist_id_for_title(
            list_owned_playlists(client),
            args.playlist_title,
        )
        uploads = list_uploads(client, channel_uploads_playlist_id(client))
        quotes = [
            item
            for item in uploads
            if is_published_youtube_quote(item["title"], item.get("description", ""))
        ]
        visible = published_video_ids(client, [item["video_id"] for item in quotes])
        quotes = [item for item in quotes if item["video_id"] in visible]
        if args.limit > 0:
            quotes = quotes[: args.limit]
        already_ids = playlist_video_ids(client, playlist_id)
    except YouTubePublishError as exc:
        if _quota_exceeded(exc):
            print(
                "Stopped: YouTube API quota is exhausted. Re-run after it resets.",
                file=sys.stderr,
            )
            return 1
        print(str(exc), file=sys.stderr)
        return 1

    added = 0
    already = 0
    pending = 0
    print(
        f"{'apply' if args.apply else 'dry-run'}: {len(quotes)} quotes, "
        f"playlist {playlist_id}"
    )
    for index, item in enumerate(quotes, start=1):
        video_id = item["video_id"]
        if video_id in already_ids:
            already += 1
            print(f"[{index}/{len(quotes)}] {video_id} already")
            continue
        pending += 1
        if not args.apply:
            print(f"[{index}/{len(quotes)}] {video_id} pending")
            continue
        try:
            client.add_video_to_playlist(video_id, playlist_id)
        except Exception as exc:
            if _quota_exceeded(exc):
                print(
                    "Stopped: YouTube API quota is exhausted. Re-run after it resets.",
                    file=sys.stderr,
                )
                print(f"added {added} already {already} pending {pending} of {len(quotes)}")
                return 1
            print(f"[{index}/{len(quotes)}] {video_id} error", file=sys.stderr)
            print(str(exc)[-300:], file=sys.stderr)
            continue
        already_ids.add(video_id)
        added += 1
        pending -= 1
        print(f"[{index}/{len(quotes)}] {video_id} added")

    print(f"added {added} already {already} pending {pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
