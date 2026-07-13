from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Literal

from PIL import Image

Platform = Literal["youtube", "instagram"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
YOUTUBE_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)
INSTAGRAM_POST_RE = re.compile(
    r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
YOUTUBE_THUMBNAIL_QUALITIES = ("maxresdefault", "hqdefault", "sddefault", "mqdefault")
MIN_THUMBNAIL_WIDTH = 480
MIN_THUMBNAIL_HEIGHT = 360
ASPECT_TOLERANCE = 0.02


class SourceThumbnailError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceThumbnailResult:
    platform: Platform
    source_url: str
    thumbnail_url: str
    destination: Path
    width: int
    height: int
    method: str


def detect_platform(url: str) -> Platform | None:
    text = url.strip()
    if parse_youtube_video_id(text):
        return "youtube"
    if parse_instagram_shortcode(text):
        return "instagram"
    return None


def parse_youtube_video_id(url: str) -> str | None:
    text = url.strip()
    match = YOUTUBE_VIDEO_ID_RE.search(text)
    if match:
        return match.group(1)
    return None


def parse_instagram_shortcode(url: str) -> str | None:
    text = url.strip()
    match = INSTAGRAM_POST_RE.search(text)
    if match:
        return match.group(1)
    return None


def youtube_thumbnail_urls(video_id: str) -> list[str]:
    return [
        f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"
        for quality in YOUTUBE_THUMBNAIL_QUALITIES
    ]


def _fetch_first_usable_thumbnail(
    urls: list[str],
    *,
    method_prefix: str,
) -> tuple[bytes, str, str]:
    last_error: Exception | None = None
    for url in urls:
        try:
            data = _download_image_bytes(url)
            image = _image_from_bytes(data)
            if _thumbnail_is_usable(image):
                quality = url.rsplit("/", 1)[-1].removesuffix(".jpg")
                return data, url, f"{method_prefix}:{quality}"
        except SourceThumbnailError as exc:
            last_error = exc
    if last_error is not None:
        raise SourceThumbnailError(
            f"No usable thumbnail found for URLs starting with {urls[0]!r}"
        ) from last_error
    raise SourceThumbnailError(
        f"No usable thumbnail found for URLs starting with {urls[0]!r}"
    )


def _thumbnail_is_usable(image: Image.Image) -> bool:
    width, height = image.size
    return width >= MIN_THUMBNAIL_WIDTH and height >= MIN_THUMBNAIL_HEIGHT


def _download_image_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise SourceThumbnailError(
            f"Failed to download thumbnail ({exc.code}): {url}"
        ) from exc
    except urllib.error.URLError as exc:
        raise SourceThumbnailError(
            f"Failed to download thumbnail: {url} ({exc.reason})"
        ) from exc


def _image_from_bytes(data: bytes) -> Image.Image:
    try:
        return Image.open(BytesIO(data))
    except OSError as exc:
        raise SourceThumbnailError("Downloaded thumbnail is not a valid image") from exc


def _save_jpeg(image: Image.Image, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rgb = image.convert("RGB")
    rgb.save(destination, format="JPEG", quality=95)


def aspects_match(width_a: int, height_a: int, width_b: int, height_b: int) -> bool:
    if width_a <= 0 or height_a <= 0 or width_b <= 0 or height_b <= 0:
        return False
    return abs((width_a / height_a) - (width_b / height_b)) <= ASPECT_TOLERANCE


def video_size_from_ytdlp_info(info: dict[str, Any]) -> tuple[int, int] | None:
    video_formats = [
        item
        for item in info.get("formats") or []
        if item.get("vcodec") not in (None, "none")
        and item.get("width")
        and item.get("height")
    ]
    if video_formats:
        best = max(video_formats, key=lambda item: int(item["width"]) * int(item["height"]))
        return int(best["width"]), int(best["height"])

    width = info.get("width")
    height = info.get("height")
    if width and height:
        return int(width), int(height)
    return None


def best_video_url_from_ytdlp_info(info: dict[str, Any]) -> str | None:
    video_formats = [
        item
        for item in info.get("formats") or []
        if item.get("vcodec") not in (None, "none")
        and item.get("width")
        and item.get("height")
        and isinstance(item.get("url"), str)
    ]
    if not video_formats:
        return None
    best = max(video_formats, key=lambda item: int(item["width"]) * int(item["height"]))
    return str(best["url"]).strip() or None


def pick_matching_thumbnail_url(
    info: dict[str, Any],
    video_size: tuple[int, int] | None,
) -> str | None:
    if video_size is None:
        default = info.get("thumbnail")
        if isinstance(default, str) and default.strip():
            return default.strip()
        return None

    video_width, video_height = video_size
    candidates: list[tuple[int, str]] = []
    for item in info.get("thumbnails") or []:
        width = item.get("width")
        height = item.get("height")
        url = item.get("url")
        if not width or not height or not isinstance(url, str) or not url.strip():
            continue
        if aspects_match(video_width, video_height, int(width), int(height)):
            candidates.append((int(width) * int(height), url.strip()))

    if candidates:
        return max(candidates, key=lambda row: row[0])[1]
    return None


def _resolve_ffmpeg_path() -> str | None:
    override = os.getenv("FFMPEG_PATH", "").strip()
    if override:
        candidate = Path(override)
        if candidate.exists():
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found

    found = shutil.which("ffmpeg")
    if found:
        return found

    try:
        from catalog_parser.ffmpeg_bundle import ensure_ffmpeg_bundled

        return str(ensure_ffmpeg_bundled().ffmpeg_path)
    except Exception:
        return None


def _extract_video_frame_bytes(video_url: str, ffmpeg_path: str) -> bytes:
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "2",
        "-i",
        video_url,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        raise SourceThumbnailError(
            "ffmpeg frame extraction failed: "
            f"{detail[:300] or 'unknown error'}"
        ) from exc
    except FileNotFoundError as exc:
        raise SourceThumbnailError("ffmpeg not found") from exc

    if not completed.stdout:
        raise SourceThumbnailError("ffmpeg produced no image data")
    return completed.stdout


def fetch_youtube_thumbnail_direct(video_id: str) -> tuple[bytes, str, str]:
    return _fetch_first_usable_thumbnail(
        youtube_thumbnail_urls(video_id),
        method_prefix="youtube-direct",
    )


def video_size_from_source_url(source_url: str) -> tuple[int, int] | None:
    info = _extract_ytdlp_info(source_url)
    return video_size_from_ytdlp_info(info)


def _extract_ytdlp_info(source_url: str) -> dict[str, Any]:
    try:
        import yt_dlp
    except ImportError as exc:
        raise SourceThumbnailError(
            "yt-dlp is required for this source URL. Install it with "
            "`pip install yt-dlp`."
        ) from exc

    options = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(source_url, download=False)
    except Exception as exc:
        message = str(exc).strip() or exc.__class__.__name__
        raise SourceThumbnailError(
            f"Could not extract metadata from {source_url!r}: {message}"
        ) from exc

    if not isinstance(info, dict):
        raise SourceThumbnailError(
            f"Could not extract metadata from {source_url!r}"
        )
    return info


def pick_best_thumbnail_url(info: dict[str, Any]) -> str | None:
    candidates: list[tuple[int, str]] = []
    for item in info.get("thumbnails") or []:
        width = item.get("width")
        height = item.get("height")
        url = item.get("url")
        if not width or not height or not isinstance(url, str) or not url.strip():
            continue
        candidates.append((int(width) * int(height), url.strip()))

    if candidates:
        return max(candidates, key=lambda row: row[0])[1]

    default = info.get("thumbnail")
    if isinstance(default, str) and default.strip():
        return default.strip()
    return None


def pick_thumbnail_url(
    info: dict[str, Any],
    video_size: tuple[int, int] | None,
) -> str | None:
    return pick_matching_thumbnail_url(info, video_size) or pick_best_thumbnail_url(info)


def _fetch_thumbnail_from_ytdlp(
    source_url: str,
    platform: Platform,
) -> tuple[bytes, str, str]:
    info = _extract_ytdlp_info(source_url)
    video_size = video_size_from_ytdlp_info(info)
    thumbnail_url = pick_thumbnail_url(info, video_size)
    if thumbnail_url is None:
        raise SourceThumbnailError(
            f"No thumbnail URL returned for {source_url!r}"
        )

    data = _download_image_bytes(thumbnail_url)
    image = _image_from_bytes(data)
    method = f"{platform}-ytdlp"
    if video_size and not aspects_match(
        video_size[0], video_size[1], image.size[0], image.size[1]
    ):
        method = f"{platform}-ytdlp-mismatch"
    return data, thumbnail_url, method


def _extract_thumbnail_url_with_ytdlp(source_url: str) -> str:
    info = _extract_ytdlp_info(source_url)
    video_size = video_size_from_ytdlp_info(info)
    thumbnail_url = pick_thumbnail_url(info, video_size)
    if thumbnail_url:
        return thumbnail_url
    raise SourceThumbnailError(
        f"No thumbnail URL returned for {source_url!r}"
    )


def _format_ytdlp_error(detail: str) -> str:
    for line in reversed(detail.splitlines()):
        stripped = line.strip()
        if stripped.startswith("ERROR:"):
            return stripped.removeprefix("ERROR:").strip()
    compact = " ".join(part.strip() for part in detail.splitlines() if part.strip())
    return compact[:300] or "unknown yt-dlp error"


def _extract_thumbnail_url_with_ytdlp_cli(source_url: str) -> str:
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--print",
        "thumbnail",
        source_url,
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SourceThumbnailError(
            f"Could not extract thumbnail URL from {source_url!r}: "
            f"{_format_ytdlp_error(detail)}"
        ) from exc
    except FileNotFoundError as exc:
        raise SourceThumbnailError(
            "yt-dlp is required for this source URL. Install it with "
            "`pip install yt-dlp`."
        ) from exc

    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise SourceThumbnailError(
            f"No thumbnail URL returned for {source_url!r}"
        )
    return lines[-1]


def extract_thumbnail_url(source_url: str) -> tuple[str, Platform, str]:
    platform = detect_platform(source_url)
    if platform is None:
        raise SourceThumbnailError(f"Unsupported original video URL: {source_url!r}")

    if platform == "youtube":
        video_id = parse_youtube_video_id(source_url)
        if video_id is None:
            raise SourceThumbnailError(
                f"Could not parse YouTube video ID from {source_url!r}"
            )
        try:
            _, thumbnail_url, method = fetch_youtube_thumbnail_direct(video_id)
            return thumbnail_url, platform, method
        except SourceThumbnailError:
            pass
        try:
            thumbnail_url = _extract_thumbnail_url_with_ytdlp(source_url)
            return thumbnail_url, platform, "youtube-ytdlp"
        except SourceThumbnailError:
            thumbnail_url = _extract_thumbnail_url_with_ytdlp_cli(source_url)
            return thumbnail_url, platform, "youtube-ytdlp-cli"

    try:
        thumbnail_url = _extract_thumbnail_url_with_ytdlp(source_url)
        return thumbnail_url, platform, "instagram-ytdlp"
    except SourceThumbnailError:
        thumbnail_url = _extract_thumbnail_url_with_ytdlp_cli(source_url)
        return thumbnail_url, platform, "instagram-ytdlp-cli"


def fetch_original_video_thumbnail(
    source_url: str,
    destination: Path,
) -> SourceThumbnailResult:
    platform = detect_platform(source_url)
    if platform is None:
        raise SourceThumbnailError(f"Unsupported original video URL: {source_url!r}")

    if platform == "youtube":
        video_id = parse_youtube_video_id(source_url)
        if video_id is None:
            raise SourceThumbnailError(
                f"Could not parse YouTube video ID from {source_url!r}"
            )
        try:
            data, thumbnail_url, method = fetch_youtube_thumbnail_direct(video_id)
        except SourceThumbnailError:
            try:
                data, thumbnail_url, method = _fetch_thumbnail_from_ytdlp(
                    source_url, platform
                )
            except SourceThumbnailError:
                thumbnail_url = _extract_thumbnail_url_with_ytdlp_cli(source_url)
                data = _download_image_bytes(thumbnail_url)
                method = "youtube-ytdlp-cli"
    else:
        try:
            data, thumbnail_url, method = _fetch_thumbnail_from_ytdlp(
                source_url, platform
            )
        except SourceThumbnailError:
            thumbnail_url = _extract_thumbnail_url_with_ytdlp_cli(source_url)
            data = _download_image_bytes(thumbnail_url)
            method = "instagram-ytdlp-cli"

    image = _image_from_bytes(data)
    if not _thumbnail_is_usable(image):
        raise SourceThumbnailError(
            f"Thumbnail from {source_url!r} is too small ({image.size[0]}x{image.size[1]})"
        )
    _save_jpeg(image, destination)
    return SourceThumbnailResult(
        platform=platform,
        source_url=source_url.strip(),
        thumbnail_url=thumbnail_url,
        destination=destination,
        width=image.size[0],
        height=image.size[1],
        method=method,
    )


def original_thumbnail_destination(download_dir: Path, video_name: str) -> Path:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", video_name).strip(" .")
    stem = cleaned or "thumbnail"
    return download_dir / f"{stem}.original-thumb.jpg"
