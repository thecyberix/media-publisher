"""Add published channel videos to the end-card playlists.

Scans the last stretch of each upload on the authorized YouTube channel.
Save Soil, Inner Engineering, and Miracle of Mind can match even when a
thumbnail frame was appended after the card. A dry run prints the matches.
Pass --apply to insert videos that are not already on the playlist.
--apply reuses the report, so matches already found are inserted without
downloading those videos again. The run stops when YouTube's daily quota
is exhausted.

Playlist titles must match config/playlists.json exactly.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.__main__ import load_env_file  # noqa: E402
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
    load_playlist_config,
    youtube_playlist_ids_for_end_cards,
)
from catalog_parser.save_soil import (  # noqa: E402
    detect_cards_in_ending_window,
    frame_looks_like_save_soil,
)
from media_publisher.config import load_settings  # noqa: E402
from media_publisher.publishers.youtube import (  # noqa: E402
    API_BASE,
    YouTubeClient,
    YouTubePublishError,
)

LOOKS_LIKE = {
    END_CARD_SAVE_SOIL: frame_looks_like_save_soil,
    END_CARD_INNER_ENGINEERING: frame_looks_like_inner_engineering,
    END_CARD_MIRACLE_OF_MIND: frame_looks_like_miracle_of_mind,
}
DEFAULT_REPORT = ROOT / "output" / "youtube-end-card-playlists.json"


def _json(client: YouTubeClient, url: str) -> dict[str, Any]:
    status, _, payload = client._request("GET", url)
    if status != 200:
        detail = payload.decode("utf-8", errors="replace").strip()
        raise YouTubePublishError(f"YouTube request failed with HTTP {status}: {detail}")
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise YouTubePublishError("YouTube response is not a JSON object")
    return data


def _paged(client: YouTubeClient, url: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token = ""
    while True:
        page_url = url
        if page_token:
            page_url = f"{url}&pageToken={urllib.parse.quote(page_token)}"
        data = _json(client, page_url)
        batch = data.get("items")
        if isinstance(batch, list):
            items.extend(item for item in batch if isinstance(item, dict))
        next_token = data.get("nextPageToken")
        if not isinstance(next_token, str) or not next_token.strip():
            return items
        page_token = next_token.strip()


def channel_uploads_playlist_id(client: YouTubeClient) -> str:
    channel = client.verify_authorized_channel()
    query = urllib.parse.urlencode({"part": "contentDetails", "id": channel.id})
    data = _json(client, f"{API_BASE}/channels?{query}")
    items = data.get("items")
    details = items[0].get("contentDetails") if isinstance(items, list) and items else None
    related = details.get("relatedPlaylists") if isinstance(details, dict) else None
    playlist_id = related.get("uploads") if isinstance(related, dict) else None
    if not isinstance(playlist_id, str) or not playlist_id.strip():
        raise YouTubePublishError("Channel has no uploads playlist")
    return playlist_id.strip()


def list_uploads(client: YouTubeClient, playlist_id: str) -> list[dict[str, str]]:
    query = urllib.parse.urlencode(
        {"part": "snippet,contentDetails", "playlistId": playlist_id, "maxResults": "50"}
    )
    uploads: list[dict[str, str]] = []
    for item in _paged(client, f"{API_BASE}/playlistItems?{query}"):
        snippet = item.get("snippet")
        content = item.get("contentDetails")
        video_id = ""
        if isinstance(content, dict) and isinstance(content.get("videoId"), str):
            video_id = content["videoId"].strip()
        if not video_id and isinstance(snippet, dict):
            resource = snippet.get("resourceId")
            if isinstance(resource, dict) and isinstance(resource.get("videoId"), str):
                video_id = resource["videoId"].strip()
        if not video_id:
            continue
        title = ""
        if isinstance(snippet, dict) and isinstance(snippet.get("title"), str):
            title = snippet["title"].strip()
        uploads.append({"video_id": video_id, "title": title or video_id})
    return uploads


def published_video_ids(client: YouTubeClient, video_ids: list[str]) -> set[str]:
    """Public and unlisted uploads. Private videos are not scanned."""
    published: set[str] = set()
    for offset in range(0, len(video_ids), 50):
        batch = video_ids[offset : offset + 50]
        query = urllib.parse.urlencode({"part": "status", "id": ",".join(batch), "maxResults": "50"})
        data = _json(client, f"{API_BASE}/videos?{query}")
        items = data.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            status = item.get("status")
            privacy = status.get("privacyStatus") if isinstance(status, dict) else None
            video_id = item.get("id")
            if privacy in {"public", "unlisted"} and isinstance(video_id, str):
                published.add(video_id)
    return published


def list_owned_playlists(client: YouTubeClient) -> list[tuple[str, str]]:
    query = urllib.parse.urlencode({"part": "snippet", "mine": "true", "maxResults": "50"})
    playlists: list[tuple[str, str]] = []
    for item in _paged(client, f"{API_BASE}/playlists?{query}"):
        playlist_id = item.get("id")
        snippet = item.get("snippet")
        title = snippet.get("title") if isinstance(snippet, dict) else None
        if isinstance(playlist_id, str) and isinstance(title, str) and title.strip():
            playlists.append((playlist_id, title.strip()))
    return playlists


def playlist_video_ids(client: YouTubeClient, playlist_id: str) -> set[str]:
    query = urllib.parse.urlencode(
        {"part": "contentDetails", "playlistId": playlist_id, "maxResults": "50"}
    )
    video_ids: set[str] = set()
    for item in _paged(client, f"{API_BASE}/playlistItems?{query}"):
        content = item.get("contentDetails")
        video_id = content.get("videoId") if isinstance(content, dict) else None
        if isinstance(video_id, str) and video_id.strip():
            video_ids.add(video_id.strip())
    return video_ids


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timed out after {timeout} seconds") from exc


def _short_error(detail: str) -> str:
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    for line in reversed(lines):
        if "http" in line.casefold() and len(line) > 180:
            continue
        return line[:300]
    return "download failed"


def download_ending(video_id: str, dest: Path, *, window_seconds: float) -> None:
    """Save the last ``window_seconds`` of a public or unlisted video.

    yt-dlp downloads a small copy, then ffmpeg cuts the ending locally.
    Seeking a googlevideo URL directly returns 403 or stalls until timeout,
    because YouTube throttles clients that are not yt-dlp.
    """
    page = f"https://www.youtube.com/watch?v={video_id}"
    full = dest.with_name(f"{dest.stem}-full{dest.suffix}")
    if full.exists():
        full.unlink()
    if dest.exists():
        dest.unlink()
    saved = None
    for _attempt in range(3):
        if full.exists():
            full.unlink()
        saved = _run(
            [
                "yt-dlp",
                "-f",
                "bv*[height<=240]/bv*",
                "--no-warnings",
                "--no-playlist",
                "-o",
                str(full),
                page,
            ],
            timeout=180,
        )
        if saved.returncode == 0 and full.is_file() and full.stat().st_size >= 1000:
            break
    if saved is None or saved.returncode != 0 or not full.is_file() or full.stat().st_size < 1000:
        detail = saved.stderr or saved.stdout if saved is not None else "yt-dlp failed"
        raise RuntimeError(_short_error(detail or "yt-dlp failed"))
    probed = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(full),
        ],
        timeout=30,
    )
    if probed.returncode != 0:
        raise RuntimeError(_short_error(probed.stderr or probed.stdout or "ffprobe failed"))
    duration = float(probed.stdout.strip())
    start = max(0.0, duration - window_seconds)
    try:
        cut = _run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(full),
                "-t",
                f"{window_seconds:.3f}",
                "-c",
                "copy",
                "-an",
                str(dest),
            ],
            timeout=60,
        )
    finally:
        full.unlink(missing_ok=True)
    if cut.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1000:
        raise RuntimeError(_short_error(cut.stderr or cut.stdout or "ffmpeg failed"))


def load_report(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("videos") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("video_id"), str):
            found[row["video_id"]] = row
    return found


def write_report(path: Path, videos: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"videos": videos}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _quota_exceeded(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    return "quotaexceeded" in detail or "youtube.quota" in detail


def _save_progress(
    path: Path,
    rows: list[dict[str, Any]],
    previous: dict[str, dict[str, Any]],
) -> None:
    seen = {item["video_id"] for item in rows}
    kept = [item for item_id, item in previous.items() if item_id not in seen]
    write_report(path, rows + kept)


def _apply_pending(
    client: YouTubeClient,
    row: dict[str, Any],
    membership: dict[str, set[str]],
    playlist_ids: dict[str, str],
) -> int:
    """Insert playlist entries recorded earlier and not yet added."""
    video_id = row.get("video_id")
    if not isinstance(video_id, str):
        return 0
    already = [key for key in row.get("already") or [] if isinstance(key, str)]
    added = [key for key in row.get("added") or [] if isinstance(key, str)]
    pending_keys = [key for key in row.get("pending") or [] if isinstance(key, str)]
    inserted = 0
    for index, key in enumerate(pending_keys):
        if key not in playlist_ids:
            continue
        if key in already or key in added or video_id in membership[key]:
            if key not in already and key not in added:
                already.append(key)
            continue
        try:
            client.add_video_to_playlist(video_id, playlist_ids[key])
        except Exception:
            row["already"] = already
            row["added"] = added
            row["pending"] = pending_keys[index:]
            raise
        membership[key].add(video_id)
        added.append(key)
        inserted += 1
    row["already"] = already
    row["added"] = added
    row["pending"] = []
    return inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert matches into the YouTube playlists, reusing the report. Without this, only report.",
    )
    parser.add_argument("--limit", type=int, default=0, help="Scan at most this many uploads.")
    parser.add_argument("--video-id", action="append", default=[], help="Scan only these ids.")
    parser.add_argument(
        "--window-seconds",
        type=float,
        default=25.0,
        help="How much of the ending to search, including a trailing thumbnail.",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip videos already recorded in the report without an error.",
    )
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
    config = load_playlist_config()
    playlist_ids = youtube_playlist_ids_for_end_cards(config.rules, list_owned_playlists(client))
    unknown = [key for key in playlist_ids if key not in LOOKS_LIKE]
    if unknown:
        print("No detector for end card: " + ", ".join(unknown), file=sys.stderr)
        return 1
    membership = {
        key: playlist_video_ids(client, playlist_id) for key, playlist_id in playlist_ids.items()
    }
    uploads = list_uploads(client, channel_uploads_playlist_id(client))
    if args.video_id:
        wanted = set(args.video_id)
        uploads = [item for item in uploads if item["video_id"] in wanted]
    visible = published_video_ids(client, [item["video_id"] for item in uploads])
    uploads = [item for item in uploads if item["video_id"] in visible]
    if args.limit > 0:
        uploads = uploads[: args.limit]

    reuse_report = args.resume or args.apply
    previous = load_report(args.report) if reuse_report else {}
    rows: list[dict[str, Any]] = []
    added = 0
    matched = 0
    print(
        f"{'apply' if args.apply else 'dry-run'}: {len(uploads)} uploads, "
        f"window {args.window_seconds:.0f}s, report {args.report}"
    )
    for index, upload in enumerate(uploads, start=1):
        video_id = upload["video_id"]
        cached = previous.get(video_id)
        if cached and not cached.get("error"):
            if args.apply and cached.get("pending"):
                try:
                    added += _apply_pending(client, cached, membership, playlist_ids)
                except Exception as exc:
                    if not _quota_exceeded(exc):
                        raise
                    rows.append(cached)
                    _save_progress(args.report, rows, previous)
                    print(
                        "Stopped: YouTube API quota is exhausted. Re-run after it resets.",
                        file=sys.stderr,
                    )
                    return 1
            rows.append(cached)
            print(f"[{index}/{len(uploads)}] {video_id} cached")
            continue
        row: dict[str, Any] = {
            "video_id": video_id,
            "title": upload["title"],
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "matches": [],
            "added": [],
            "already": [],
            "pending": [],
        }
        try:
            with tempfile.TemporaryDirectory(prefix="yt-endcard-") as tmp:
                tail = Path(tmp) / "tail.mp4"
                download_ending(video_id, tail, window_seconds=args.window_seconds)
                found = detect_cards_in_ending_window(
                    tail,
                    LOOKS_LIKE,
                    work_dir=Path(tmp),
                    window_seconds=args.window_seconds,
                )
            row["matches"] = [
                {"end_card": key, "start_seconds": start} for key, start in sorted(found.items())
            ]
            for key in found:
                if video_id in membership[key]:
                    row["already"].append(key)
                    continue
                row["pending"].append(key)
                if args.apply:
                    client.add_video_to_playlist(video_id, playlist_ids[key])
                    membership[key].add(video_id)
                    row["added"].append(key)
                    row["pending"].remove(key)
                    added += 1
            if found:
                matched += 1
            print(
                f"[{index}/{len(uploads)}] {video_id} "
                + (", ".join(found) if found else "none")
            )
        except Exception as exc:
            if _quota_exceeded(exc):
                rows.append(row)
                _save_progress(args.report, rows, previous)
                print(
                    "Stopped: YouTube API quota is exhausted. Re-run after it resets.",
                    file=sys.stderr,
                )
                return 1
            row["error"] = str(exc)[-500:]
            print(f"[{index}/{len(uploads)}] {video_id} error")
        rows.append(row)
        _save_progress(args.report, rows, previous)

    print(f"matched {matched} added {added} scanned {len(rows)}")
    print(f"report {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
