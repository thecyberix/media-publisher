from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class ImageVideoError(RuntimeError):
    pass


SHORT_VIDEO_WIDTH = 1080
SHORT_VIDEO_HEIGHT = 1920
SHORT_COVER_INTRO_SECONDS = 5.0
# Backward-compatible alias for older imports and env names.
SHORT_COVER_END_SECONDS = SHORT_COVER_INTRO_SECONDS
QUOTE_VIDEO_DURATION_SECONDS = 10.0


def _resolve_ffmpeg(ffmpeg_path: str | None = None) -> str:
    ffmpeg = ffmpeg_path or shutil.which("ffmpeg")
    if not ffmpeg:
        raise ImageVideoError(
            "ffmpeg is required for quote videos and Short cover intros; "
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


def _video_has_audio_stream(video_path: Path) -> bool:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return True

    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return bool(result.stdout.strip())


def _short_cover_intro_filter(*, delay_ms: int, include_audio: bool) -> str:
    width = SHORT_VIDEO_WIDTH
    height = SHORT_VIDEO_HEIGHT
    parts = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30,format=yuv420p[intro];"
        f"[1:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[main];"
        f"[intro][main]concat=n=2:v=1:a=0[outv]",
    ]
    if include_audio:
        parts.append(
            f";[1:a]adelay={delay_ms}|{delay_ms},aresample=async=1:first_pts=0[aout]"
        )
    return "".join(parts)


def ensure_short_with_cover_intro(
    video_path: Path,
    thumbnail_path: Path,
    *,
    ffmpeg_path: str | None = None,
    intro_seconds: float = SHORT_COVER_INTRO_SECONDS,
) -> Path:
    """Prepend a static cover clip before a Short upload.

    Intended for subtitle-burned catalog videos: run this after HappyScribe/ffmpeg
    has baked subtitles into the main clip so the text stays aligned with speech.
    """
    source_video = video_path.resolve()
    source_thumb = thumbnail_path.resolve()
    if not source_video.is_file():
        raise ImageVideoError(f"Video file not found: {source_video}")
    if not source_thumb.is_file():
        raise ImageVideoError(f"Thumbnail file not found: {source_thumb}")
    if intro_seconds <= 0:
        raise ImageVideoError("intro_seconds must be greater than zero")

    ffmpeg = _resolve_ffmpeg(ffmpeg_path)
    destination = source_video.with_name(f"{source_video.stem}.youtube-short-cover-intro.mp4")
    newest_source = max(source_video.stat().st_mtime, source_thumb.stat().st_mtime)
    if destination.is_file() and destination.stat().st_mtime >= newest_source:
        return destination

    has_audio = _video_has_audio_stream(source_video)
    delay_ms = int(round(intro_seconds * 1000))
    filter_complex = _short_cover_intro_filter(
        delay_ms=delay_ms,
        include_audio=has_audio,
    )

    command = [
        ffmpeg,
        "-y",
        "-loop",
        "1",
        "-framerate",
        "30",
        "-t",
        str(intro_seconds),
        "-i",
        str(source_thumb),
        "-i",
        str(source_video),
        "-filter_complex",
        filter_complex,
        "-map",
        "[outv]",
    ]
    if has_audio:
        command.extend(["-map", "[aout]"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        command.extend(["-c:a", "aac", "-ar", "48000"])
    command.extend(["-movflags", "+faststart", str(destination)])
    _run_ffmpeg(command, action=f"prepend cover intro for {source_video.name}")

    if not destination.is_file():
        raise ImageVideoError(f"ffmpeg did not create Short cover file: {destination}")
    return destination


def ensure_short_with_cover_at_end(
    video_path: Path,
    thumbnail_path: Path,
    *,
    ffmpeg_path: str | None = None,
    outro_seconds: float = SHORT_COVER_INTRO_SECONDS,
) -> Path:
    """Backward-compatible alias for :func:`ensure_short_with_cover_intro`."""
    return ensure_short_with_cover_intro(
        video_path,
        thumbnail_path,
        ffmpeg_path=ffmpeg_path,
        intro_seconds=outro_seconds,
    )


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
