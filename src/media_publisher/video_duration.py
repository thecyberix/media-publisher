from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from media_publisher.sources.airtable import FIELD_DURATION

MAX_INSTAGRAM_VIDEO_SECONDS = 15 * 60
INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES = 8 * 1024 * 1024
INSTAGRAM_REENCODE_MAX_BYTES = 40 * 1024 * 1024
INSTAGRAM_REENCODE_AUDIO_BITRATE = "128k"


class InstagramVideoPrepError(RuntimeError):
    pass
_DURATION_TEXT_RE = re.compile(
    r"^(?:(?P<hours>\d+):)?(?P<minutes>\d+):(?P<seconds>\d+(?:\.\d+)?)$"
)


def parse_duration_seconds(value: object) -> float | None:
    """Parse a duration from Airtable or metadata into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if seconds > 0 else None

    text = str(value).strip()
    if not text:
        return None

    try:
        seconds = float(text.replace(",", "."))
    except ValueError:
        seconds = None
    if seconds is not None and seconds > 0:
        return seconds

    match = _DURATION_TEXT_RE.match(text)
    if not match:
        return None

    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = float(match.group("seconds"))
    total = hours * 3600 + minutes * 60 + seconds
    return total if total > 0 else None


def probe_local_video_duration_seconds(video_path: Path) -> float | None:
    path = video_path.resolve()
    if not path.is_file():
        return None

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return None

    try:
        seconds = float(result.stdout.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def resolve_video_duration_seconds(
    *,
    video_path: Path | str | None = None,
    metadata: dict[str, str] | None = None,
) -> float | None:
    if metadata:
        duration = parse_duration_seconds(metadata.get(FIELD_DURATION))
        if duration is not None:
            return duration

    if video_path is not None:
        return probe_local_video_duration_seconds(Path(video_path))
    return None


def instagram_exceeds_api_limit(duration_seconds: float | None) -> bool:
    return (
        duration_seconds is not None
        and duration_seconds > MAX_INSTAGRAM_VIDEO_SECONDS
    )


def instagram_duration_skip_message(duration_seconds: float) -> str:
    minutes = duration_seconds / 60
    return (
        f"instagram: skipped — video is {minutes:.1f} minutes; "
        "Instagram Graph API Reels limit is 15 minutes"
    )


def _resolve_ffmpeg(ffmpeg_path: str | None = None) -> str:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        raise InstagramVideoPrepError(
            "ffmpeg is required to prepare Instagram uploads; "
            "install ffmpeg or set HAPPYSCRIBE_FFMPEG"
        )
    return ffmpeg


def instagram_upload_cache_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}-ig-upload{source.suffix}")


def instagram_reencode_cache_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}-ig-reencode{source.suffix}")


def _instagram_video_bitrate(duration_seconds: float, *, max_bytes: int) -> str:
    audio_bps = 128_000
    total_bps = (max_bytes * 8) / max(duration_seconds, 1.0)
    video_bps = max(int(total_bps - audio_bps), 80_000)
    return f"{max(video_bps // 1000, 80)}k"


def reencode_instagram_upload_video(
    source: Path,
    *,
    ffmpeg_path: str | None = None,
    max_bytes: int = INSTAGRAM_REENCODE_MAX_BYTES,
    force: bool = False,
) -> Path:
    """Re-encode a local MP4 for Instagram rupload with faststart moov."""
    path = source.resolve()
    if not path.is_file():
        raise InstagramVideoPrepError(f"Video file not found: {path}")

    destination = instagram_reencode_cache_path(path)
    if (
        not force
        and destination.is_file()
        and destination.stat().st_mtime >= path.stat().st_mtime
    ):
        return destination

    duration_seconds = probe_local_video_duration_seconds(path) or 60.0
    video_bitrate = _instagram_video_bitrate(duration_seconds, max_bytes=max_bytes)
    maxrate = f"{max(int(video_bitrate.rstrip('k')) * 3 // 2, 600)}k"
    bufsize = f"{max(int(video_bitrate.rstrip('k')) * 3, 1200)}k"

    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-profile:v",
            "main",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "60",
            "-keyint_min",
            "60",
            "-sc_threshold",
            "0",
            "-b:v",
            video_bitrate,
            "-maxrate",
            maxrate,
            "-bufsize",
            bufsize,
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease",
            "-c:a",
            "aac",
            "-b:a",
            INSTAGRAM_REENCODE_AUDIO_BITRATE,
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise InstagramVideoPrepError(
            f"ffmpeg failed to re-encode Instagram upload for {path.name}: {detail}"
        )
    if not destination.is_file():
        raise InstagramVideoPrepError(
            f"ffmpeg did not create Instagram re-encode file: {destination}"
        )
    return destination


def ensure_instagram_upload_video(
    source: Path,
    *,
    ffmpeg_path: str | None = None,
) -> Path:
    """Remux a local MP4 with moov at the front for Instagram rupload."""
    path = source.resolve()
    if not path.is_file():
        raise InstagramVideoPrepError(f"Video file not found: {path}")

    destination = instagram_upload_cache_path(path)
    if destination.is_file() and destination.stat().st_mtime >= path.stat().st_mtime:
        return destination

    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise InstagramVideoPrepError(
            f"ffmpeg failed to prepare Instagram upload for {path.name}: {detail}"
        )
    if not destination.is_file():
        raise InstagramVideoPrepError(
            f"ffmpeg did not create Instagram upload file: {destination}"
        )
    return destination
