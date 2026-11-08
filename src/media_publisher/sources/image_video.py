from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ImageVideoError(RuntimeError):
    pass


QUOTE_VIDEO_DURATION_SECONDS = 10.0


def _resolve_ffmpeg(ffmpeg_path: str | None = None) -> str:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        raise ImageVideoError(
            "ffmpeg is required for quote videos; "
            "install ffmpeg or set HAPPYSCRIBE_FFMPEG"
        )
    return ffmpeg


def _run_ffmpeg(command: list[str], *, action: str) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ImageVideoError(f"ffmpeg failed to {action}: {detail}") from exc


def image_to_quote_video(
    image_path: Path,
    destination: Path,
    *,
    ffmpeg_path: str | None = None,
    duration_seconds: float = QUOTE_VIDEO_DURATION_SECONDS,
) -> Path:
    """Convert a still quote image into a vertical MP4 for YouTube Shorts."""
    source = image_path.resolve()
    if not source.is_file():
        raise ImageVideoError(f"Image file not found: {source}")

    ffmpeg = _resolve_ffmpeg(ffmpeg_path)

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)

    command = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-i",
        str(source),
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        "-t",
        str(duration_seconds),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-vf",
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
        "-shortest",
        str(destination),
    ]
    _run_ffmpeg(command, action=f"convert {source.name} to video")

    if not destination.is_file():
        raise ImageVideoError(f"ffmpeg did not create output file: {destination}")
    return destination


def ensure_quote_video(
    image_path: Path,
    work_dir: Path,
    *,
    ffmpeg_path: str | None = None,
    duration_seconds: float = QUOTE_VIDEO_DURATION_SECONDS,
) -> Path:
    """Build or reuse a cached MP4 derived from a quote image."""
    work_dir.mkdir(parents=True, exist_ok=True)
    duration_label = int(duration_seconds) if duration_seconds == int(duration_seconds) else duration_seconds
    destination = work_dir / f"{image_path.stem}_quote_{duration_label}s.mp4"
    if destination.is_file():
        if destination.stat().st_mtime >= image_path.stat().st_mtime:
            return destination
    return image_to_quote_video(
        image_path,
        destination,
        ffmpeg_path=ffmpeg_path,
        duration_seconds=duration_seconds,
    )
