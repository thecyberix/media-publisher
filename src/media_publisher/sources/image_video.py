from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ImageVideoError(RuntimeError):
    pass


SHORT_VIDEO_WIDTH = 1080
SHORT_VIDEO_HEIGHT = 1920
SHORT_COVER_END_SECONDS = 2.0


def _resolve_ffmpeg(ffmpeg_path: str | None = None) -> str:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        raise ImageVideoError(
            "ffmpeg is required for quote videos and Short cover outros; "
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


def _short_cover_end_filter() -> str:
    width = SHORT_VIDEO_WIDTH
    height = SHORT_VIDEO_HEIGHT
    return (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[main];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,format=yuv420p[outro];"
        f"[main][outro]concat=n=2:v=1:a=0[outv]"
    )


def ensure_short_with_cover_at_end(
    video_path: Path,
    thumbnail_path: Path,
    *,
    ffmpeg_path: str | None = None,
    outro_seconds: float = SHORT_COVER_END_SECONDS,
) -> Path:
    """Append a static cover clip so the frame can be picked in the YouTube mobile app."""
    source_video = video_path.resolve()
    source_thumb = thumbnail_path.resolve()
    if not source_video.is_file():
        raise ImageVideoError(f"Video file not found: {source_video}")
    if not source_thumb.is_file():
        raise ImageVideoError(f"Thumbnail file not found: {source_thumb}")

    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    destination = source_video.with_name(f"{source_video.stem}.youtube-short-cover-end.mp4")
    newest_source = max(source_video.stat().st_mtime, source_thumb.stat().st_mtime)
    if destination.is_file() and destination.stat().st_mtime >= newest_source:
        return destination

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source_video),
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        str(outro_seconds),
        "-i",
        str(source_thumb),
        "-filter_complex",
        _short_cover_end_filter(),
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(destination),
    ]
    _run_ffmpeg(command, action=f"append cover outro for {source_video.name}")

    if not destination.is_file():
        raise ImageVideoError(f"ffmpeg did not create Short cover file: {destination}")
    return destination


def image_to_quote_video(
    image_path: Path,
    destination: Path,
    *,
    ffmpeg_path: str | None = None,
    duration_seconds: float = 5.0,
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
) -> Path:
    """Build or reuse a cached MP4 derived from a quote image."""
    work_dir.mkdir(parents=True, exist_ok=True)
    destination = work_dir / f"{image_path.stem}_quote.mp4"
    if destination.is_file():
        if destination.stat().st_mtime >= image_path.stat().st_mtime:
            return destination
    return image_to_quote_video(
        image_path,
        destination,
        ffmpeg_path=ffmpeg_path,
    )
