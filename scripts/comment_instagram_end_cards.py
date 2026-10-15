"""Comment on Instagram videos whose ending matches a configured end card.

The comment comes from config/playlists.json for that card. Save Soil has no
comment, so those videos are reported and left unchanged. A dry run prints
the matches. Pass --apply to post. Pass --url to try one Instagram video
before a full run. The run stops when Instagram rate-limits the request.
This does not call Airtable.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.inner_engineering import (  # noqa: E402
    frame_looks_like_inner_engineering,
)
from catalog_parser.miracle_of_mind import (  # noqa: E402
    frame_looks_like_miracle_of_mind,
)
from catalog_parser.playlists import (  # noqa: E402
    END_CARD_INNER_ENGINEERING,
    END_CARD_MIRACLE_OF_MIND,
    END_CARD_SAVE_SOIL,
    instagram_comments_for_end_cards,
    load_playlist_config,
)
from catalog_parser.save_soil import (  # noqa: E402
    detect_cards_in_ending_window,
    frame_looks_like_save_soil,
)
from media_publisher.config import load_settings  # noqa: E402
from media_publisher.publishers.meta import MetaClient, MetaError  # noqa: E402

LOOKS_LIKE = {
    END_CARD_INNER_ENGINEERING: frame_looks_like_inner_engineering,
    END_CARD_SAVE_SOIL: frame_looks_like_save_soil,
    END_CARD_MIRACLE_OF_MIND: frame_looks_like_miracle_of_mind,
}
DEFAULT_REPORT = ROOT / "output" / "instagram-end-card-comments.json"
_IG_SHORTCODE = re.compile(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)", re.IGNORECASE)


def _rate_limited(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    if "rate limit" in detail or "too many calls" in detail or "request limit" in detail:
        return True
    if isinstance(exc, MetaError):
        code = (exc.payload or {}).get("code")
        return code in {4, 17, 32, 613}
    return False


def _remember_commented_video(media_id: str, end_card: str, permalink: object) -> None:
    from media_publisher.instagram_comment_responses import (
        DEFAULT_STATE_RELATIVE_PATH,
        remember_commented_video,
    )

    remember_commented_video(
        ROOT / DEFAULT_STATE_RELATIVE_PATH,
        media_id=media_id,
        end_cards=[end_card],
        permalink=permalink if isinstance(permalink, str) else "",
    )


def _shortcode(value: str) -> str | None:
    match = _IG_SHORTCODE.search(value.strip())
    return match.group(1) if match else None


def list_instagram_videos(
    client: MetaClient,
    instagram_account_id: str,
    *,
    limit: int = 0,
    media_ids: set[str] | None = None,
    shortcodes: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Newest videos and reels on the account.

    When ``media_ids`` or ``shortcodes`` is set, paging stops once each requested
    video has been found.
    """
    wanted_ids = set(media_ids or [])
    wanted_codes = {code.casefold() for code in (shortcodes or [])}
    selective = bool(wanted_ids or wanted_codes)
    videos: list[dict[str, Any]] = []
    query: dict[str, str] = {
        "fields": "id,media_type,media_product_type,permalink,media_url",
        "limit": "50",
    }
    while True:
        payload = client._request("GET", f"{instagram_account_id}/media", query=query)
        if not isinstance(payload, dict):
            break
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            media_type = item.get("media_type")
            product = item.get("media_product_type")
            if media_type != "VIDEO" and product != "REELS":
                continue
            if selective:
                code = _shortcode(str(item.get("permalink") or ""))
                media_id = item.get("id")
                if media_id not in wanted_ids and (code or "").casefold() not in wanted_codes:
                    continue
            videos.append(item)
            if selective and _requested_videos_found(videos, wanted_ids, wanted_codes):
                return videos
            if not selective and limit and len(videos) >= limit:
                return videos
        after = ((payload.get("paging") or {}).get("cursors") or {}).get("after")
        if not isinstance(after, str) or not after:
            break
        query = {**query, "after": after}
    return videos


def _requested_videos_found(
    videos: list[dict[str, Any]],
    media_ids: set[str],
    shortcodes: set[str],
) -> bool:
    found_ids = {item.get("id") for item in videos}
    found_codes = {
        code.casefold()
        for item in videos
        if (code := _shortcode(str(item.get("permalink") or "")))
    }
    return media_ids <= found_ids and shortcodes <= found_codes


def comment_texts(client: MetaClient, media_id: str) -> list[str]:
    payload = client._request(
        "GET",
        f"{media_id}/comments",
        query={"fields": "text", "limit": "50"},
    )
    if not isinstance(payload, dict):
        return []
    texts: list[str] = []
    for item in payload.get("data") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            texts.append(item["text"])
    return texts


def download_media(url: str, dest: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            dest.write_bytes(response.read())
    except Exception as exc:
        raise RuntimeError(f"download failed: {exc}") from exc
    if dest.stat().st_size < 1000:
        raise RuntimeError("download failed")


def load_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("media_id"), str):
            found[row["media_id"]] = row
    return found


def write_report(path: Path, videos: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"videos": videos}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _save_progress(
    path: Path,
    rows: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> None:
    seen = {item["media_id"] for item in rows}
    kept = [item for media_id, item in previous.items() if media_id not in seen]
    write_report(path, rows + kept)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Post the matching comment. Without this, only report.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Scan at most this many videos.")
    parser.add_argument(
        "--url",
        action="append",
        default=[],
        help="Scan only these Instagram video URLs. Repeat for more than one.",
    )
    parser.add_argument(
        "--media-id",
        action="append",
        default=[],
        help="Scan only these Instagram media ids. Repeat for more than one.",
    )
    parser.add_argument("--window-seconds", type=float, default=25.0)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip videos already recorded in the report without an error.",
    )
    args = parser.parse_args()

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
    _page_id, instagram_account_id, _page = resolve_meta_targets(settings)
    config = load_playlist_config()
    shortcodes = {code for url in args.url if (code := _shortcode(url))}
    invalid_urls = [url for url in args.url if _shortcode(url) is None]
    if invalid_urls:
        print("Not an Instagram video URL: " + ", ".join(invalid_urls), file=sys.stderr)
        return 1
    videos = list_instagram_videos(
        client,
        instagram_account_id,
        limit=args.limit,
        media_ids=set(args.media_id),
        shortcodes=shortcodes,
    )
    if args.url or args.media_id:
        found_ids = {item.get("id") for item in videos}
        found_codes = {
            code.casefold()
            for item in videos
            if (code := _shortcode(str(item.get("permalink") or "")))
        }
        missing = [item for item in args.media_id if item not in found_ids]
        missing.extend(
            url for url in args.url if (_shortcode(url) or "").casefold() not in found_codes
        )
        if missing:
            print("Instagram video not found: " + ", ".join(missing), file=sys.stderr)
            return 1

    reuse_report = args.resume or args.apply
    previous = load_report(args.report) if reuse_report else {}
    rows: list[dict[str, Any]] = []
    posted = 0
    matched = 0
    print(
        f"{'apply' if args.apply else 'dry-run'}: {len(videos)} videos, "
        f"window {args.window_seconds:.0f}s, report {args.report}"
    )
    for index, item in enumerate(videos, start=1):
        media_id = item.get("id")
        if not isinstance(media_id, str) or not media_id:
            continue
        cached = previous.get(media_id)
        if cached and not cached.get("error"):
            updated = False
            if args.apply and cached.get("pending"):
                try:
                    messages = dict(
                        instagram_comments_for_end_cards(cached["pending"], config=config)
                    )
                    existing = comment_texts(client, media_id)
                    for key in list(cached.get("pending") or []):
                        message = messages.get(key)
                        if not message:
                            continue
                        if message in existing:
                            cached.setdefault("already", []).append(key)
                            cached["pending"].remove(key)
                            continue
                        client.create_instagram_comment(media_id=media_id, message=message)
                        cached.setdefault("posted", []).append(key)
                        cached["pending"].remove(key)
                        _remember_commented_video(media_id, key, cached.get("permalink"))
                        posted += 1
                except Exception as exc:
                    if not _rate_limited(exc):
                        raise
                    rows.append(cached)
                    _save_progress(args.report, rows, previous)
                    print(
                        "Stopped: Instagram rate limit reached. Re-run after it resets.",
                        file=sys.stderr,
                    )
                    return 1
                updated = True
            rows.append(cached)
            if updated:
                _save_progress(args.report, rows, previous)
            print(f"[{index}/{len(videos)}] {media_id} cached")
            continue
        row: dict[str, Any] = {
            "media_id": media_id,
            "permalink": item.get("permalink") or "",
            "matches": [],
            "posted": [],
            "already": [],
            "pending": [],
        }
        try:
            media_url = item.get("media_url")
            if not isinstance(media_url, str) or not media_url:
                raise RuntimeError("Instagram did not return a video URL")
            with tempfile.TemporaryDirectory(prefix="ig-endcard-") as tmp:
                video_path = Path(tmp) / "video.mp4"
                download_media(media_url, video_path)
                found = detect_cards_in_ending_window(
                    video_path,
                    LOOKS_LIKE,
                    work_dir=Path(tmp),
                    window_seconds=args.window_seconds,
                )
            comments = instagram_comments_for_end_cards(found, config=config)
            row["matches"] = [
                {"end_card": key, "start_seconds": start} for key, start in sorted(found.items())
            ]
            existing = comment_texts(client, media_id) if comments else []
            for key, message in comments:
                if message in existing:
                    row["already"].append(key)
                    _remember_commented_video(media_id, key, row.get("permalink"))
                    continue
                row["pending"].append(key)
                if args.apply:
                    client.create_instagram_comment(media_id=media_id, message=message)
                    row["posted"].append(key)
                    row["pending"].remove(key)
                    _remember_commented_video(media_id, key, row.get("permalink"))
                    posted += 1
            if found:
                matched += 1
            printable = sorted(found)
            print(
                f"[{index}/{len(videos)}] {media_id} "
                + (", ".join(printable) if printable else "none")
            )
        except Exception as exc:
            if _rate_limited(exc):
                rows.append(row)
                _save_progress(args.report, rows, previous)
                print(
                    "Stopped: Instagram rate limit reached. Re-run after it resets.",
                    file=sys.stderr,
                )
                return 1
            row["error"] = str(exc)[-500:]
            print(f"[{index}/{len(videos)}] {media_id} error")
        rows.append(row)
        _save_progress(args.report, rows, previous)

    print(f"matched {matched} posted {posted} scanned {len(rows)}")
    print(f"report {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
