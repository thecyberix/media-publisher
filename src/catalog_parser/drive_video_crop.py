"""Crop encoded letterbox/pillarbox bars to the orientation a video type needs.

Phone Reels are often delivered as 1920x1080 with a 9:16 picture and black
sides. Drive/ffprobe then report landscape. Ingest and combine treat a stable
center crop to 9:16 (or 16:9) as matching the required orientation.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from catalog_parser.drive_combine import (
    DriveCombineError,
    DriveMediaFile,
    download_drive_file,
    resolve_ffmpeg_path,
)
from googleapiclient.discovery import Resource

VideoOrientation = Literal["horizontal", "vertical"]
from media_publisher.video_duration import (
    probe_local_video_duration_seconds,
    probe_local_video_size,
)

VERTICAL_ASPECT = 9 / 16
# 9:16 width/height, with slack for cropdetect rounding.
_VERTICAL_ASPECT_TOLERANCE = 0.08
_MIN_SIDE_BAR_FRACTION = 0.12
_MAX_BAR_ASYMMETRY_FRACTION = 0.15
_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")
VERTICAL_OUTPUT_SIZE = (1080, 1920)
HORIZONTAL_OUTPUT_SIZE = (1920, 1080)


@dataclass(frozen=True)
class CropRect:
    width: int
    height: int
    x: int
    y: int

    def ffmpeg_filter(self) -> str:
        return f"crop={self.width}:{self.height}:{self.x}:{self.y}"


def _even_down(value: int) -> int:
    return value if value % 2 == 0 else max(0, value - 1)


def _orientation_from_size(width: int, height: int) -> VideoOrientation | None:
    if width > height:
        return "horizontal"
    if height > width:
        return "vertical"
    return None


def parse_cropdetect_crop(text: str) -> CropRect | None:
    match = None
    for match in _CROP_RE.finditer(text or ""):
        pass
    if match is None:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    if width <= 0 or height <= 0:
        return None
    return CropRect(width=width, height=height, x=x, y=y)


def _median_int(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def median_crop(samples: list[CropRect]) -> CropRect | None:
    if not samples:
        return None
    return CropRect(
        width=_median_int([item.width for item in samples]),
        height=_median_int([item.height for item in samples]),
        x=_median_int([item.x for item in samples]),
        y=_median_int([item.y for item in samples]),
    )


def looks_like_encoded_bars(
    canvas_width: int,
    canvas_height: int,
    content: CropRect,
    required: VideoOrientation,
) -> bool:
    if canvas_width <= 0 or canvas_height <= 0:
        return False
    canvas_orientation = _orientation_from_size(canvas_width, canvas_height)
    if canvas_orientation is None or canvas_orientation == required:
        return False

    if required == "vertical":
        aspect = content.width / canvas_height
        if abs(aspect - VERTICAL_ASPECT) > _VERTICAL_ASPECT_TOLERANCE:
            return False
        left = content.x
        right = canvas_width - content.x - content.width
        if left < _MIN_SIDE_BAR_FRACTION * canvas_width:
            return False
        if right < _MIN_SIDE_BAR_FRACTION * canvas_width:
            return False
        if abs(left - right) > _MAX_BAR_ASYMMETRY_FRACTION * canvas_width:
            return False
        return True

    aspect = content.height / canvas_width
    if abs(aspect - VERTICAL_ASPECT) > _VERTICAL_ASPECT_TOLERANCE:
        return False
    top = content.y
    bottom = canvas_height - content.y - content.height
    if top < _MIN_SIDE_BAR_FRACTION * canvas_height:
        return False
    if bottom < _MIN_SIDE_BAR_FRACTION * canvas_height:
        return False
    if abs(top - bottom) > _MAX_BAR_ASYMMETRY_FRACTION * canvas_height:
        return False
    return True


def fixed_aspect_crop(
    canvas_width: int,
    canvas_height: int,
    required: VideoOrientation,
    content: CropRect,
) -> CropRect:
    """Largest even target-aspect rectangle on the canvas, centered on content."""
    if required == "vertical":
        height = _even_down(canvas_height)
        width = _even_down(int(round(height * VERTICAL_ASPECT)))
        center_x = content.x + content.width / 2
        x = int(round(center_x - width / 2))
        x = max(0, min(x, canvas_width - width))
        x = _even_down(x)
        if x + width > canvas_width:
            x = _even_down(canvas_width - width)
        return CropRect(width=width, height=height, x=max(0, x), y=0)

    width = _even_down(canvas_width)
    height = _even_down(int(round(width * VERTICAL_ASPECT)))
    center_y = content.y + content.height / 2
    y = int(round(center_y - height / 2))
    y = max(0, min(y, canvas_height - height))
    y = _even_down(y)
    if y + height > canvas_height:
        y = _even_down(canvas_height - height)
    return CropRect(width=width, height=height, x=0, y=max(0, y))


def _cropdetect_sample_times(duration: float | None) -> list[float]:
    if duration is None or duration <= 3:
        return [0.5]
    last = max(duration - 1.5, 1.0)
    points = [1.0, duration * 0.25, duration * 0.5, duration * 0.75, last]
    unique: list[float] = []
    for point in points:
        clamped = min(max(point, 0.4), last)
        if all(abs(clamped - seen) > 0.4 for seen in unique):
            unique.append(clamped)
    return unique[:5]


def _run_cropdetect(
    video_path: Path,
    *,
    start: float,
    ffmpeg: str,
) -> CropRect | None:
    result = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-ss",
            f"{start:.3f}",
            "-t",
            "1.2",
            "-i",
            str(video_path),
            "-vf",
            "cropdetect=24:2:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return parse_cropdetect_crop(result.stderr or "")


def detect_orientation_crop(
    video_path: Path,
    required: VideoOrientation,
    *,
    ffmpeg_path: str | None = None,
) -> CropRect | None:
    """Return a fixed 9:16 or 16:9 crop when encoded bars match ``required``."""
    size = probe_local_video_size(video_path)
    if size is None:
        return None
    canvas_width, canvas_height = size
    native = _orientation_from_size(canvas_width, canvas_height)
    if native == required:
        return None
    try:
        ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    except DriveCombineError:
        return None

    duration = probe_local_video_duration_seconds(video_path)
    samples: list[CropRect] = []
    for start in _cropdetect_sample_times(duration):
        sample = _run_cropdetect(video_path, start=start, ffmpeg=ffmpeg)
        if sample is not None:
            samples.append(sample)
    min_samples = 1 if duration is not None and duration < 4 else 2
    if len(samples) < min_samples:
        return None
    widths = [item.width for item in samples]
    if max(widths) - min(widths) > 96:
        return None
    content = median_crop(samples)
    if content is None:
        return None
    if not looks_like_encoded_bars(canvas_width, canvas_height, content, required):
        return None
    return fixed_aspect_crop(canvas_width, canvas_height, required, content)


def apply_orientation_crop(
    video_path: Path,
    crop: CropRect,
    destination: Path,
    *,
    required: VideoOrientation,
    ffmpeg_path: str | None = None,
) -> Path:
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    out_w, out_h = (
        VERTICAL_OUTPUT_SIZE if required == "vertical" else HORIZONTAL_OUTPUT_SIZE
    )
    video_filter = f"{crop.ffmpeg_filter()},scale={out_w}:{out_h}:flags=lanczos"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(video_path),
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0 or not destination.exists():
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise DriveCombineError(f"ffmpeg crop failed: {detail}")
    return destination


def drive_video_has_orientation_crop(
    drive_service: Resource,
    video: DriveMediaFile,
    required: VideoOrientation,
    *,
    ffmpeg_path: str | None = None,
) -> bool:
    suffix = Path(video.name).suffix or ".mp4"
    with tempfile.TemporaryDirectory(prefix="orient-crop-") as tmp:
        local = Path(tmp) / f"probe{suffix}"
        try:
            download_drive_file(drive_service, video.id, local)
        except Exception:
            return False
        return (
            detect_orientation_crop(
                local,
                required,
                ffmpeg_path=ffmpeg_path,
            )
            is not None
        )


def prepare_merge_video_orientation(
    video_path: Path,
    required: VideoOrientation | None,
    work_dir: Path,
    *,
    ffmpeg_path: str | None = None,
) -> tuple[Path, CropRect | None]:
    """Crop+scale when the file is the wrong canvas orientation with encoded bars."""
    if required is None:
        return video_path, None
    crop = detect_orientation_crop(
        video_path,
        required,
        ffmpeg_path=ffmpeg_path,
    )
    if crop is None:
        return video_path, None
    destination = work_dir / f"{video_path.stem}.orient-crop.mp4"
    apply_orientation_crop(
        video_path,
        crop,
        destination,
        required=required,
        ffmpeg_path=ffmpeg_path,
    )
    return destination, crop
