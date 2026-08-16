"""Replace English SAVE SOIL end cards during Combined Media generation."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from googleapiclient.discovery import Resource
from PIL import Image

from catalog_parser.drive_combine import (
    DriveCombineError,
    DriveMediaFile,
    download_drive_file,
    resolve_ffmpeg_path,
)
from catalog_parser.drive_media import (
    FOLDER_MIME_TYPE,
    list_folder_children,
    resolve_drive_item,
)
from catalog_parser.parser import TYPE_VIDEO, parse_video_type
from media_publisher.sources.drive_layout import (
    FOLDER_IMAGES,
    resolve_save_soil_folder_id,
)
from media_publisher.video_duration import (
    probe_local_video_duration_seconds,
    probe_local_video_size,
)

SAVE_SOIL_REEL_STEM = "savesoilreel"
SAVE_SOIL_VIDEO_STEM = "savesoilvideo"
_STEM_RE = re.compile(r"[^a-z0-9]+")

VideoOrientation = Literal["horizontal", "vertical"]

# End cards in source packages are a few seconds; search the last 20s.
_DETECT_WINDOW_SECONDS = 20.0
_MIN_END_CARD_SECONDS = 1.0
_FRAME_SCALE = 180


def _normalize_stem(name: str) -> str:
    return _STEM_RE.sub("", Path(name).stem.casefold())


def save_soil_image_kind(name: str) -> VideoOrientation | None:
    """Return orientation encoded by SaveSoilReel / SaveSoilVideo filenames."""
    stem = _normalize_stem(name)
    if stem == SAVE_SOIL_REEL_STEM or stem.startswith(SAVE_SOIL_REEL_STEM):
        return "vertical"
    if stem == SAVE_SOIL_VIDEO_STEM or stem.startswith(SAVE_SOIL_VIDEO_STEM):
        return "horizontal"
    return None


def orientation_for_video_type(video_type: str | None) -> VideoOrientation | None:
    if not video_type or not str(video_type).strip():
        return None
    try:
        parsed = parse_video_type(video_type)
    except ValueError:
        return None
    if parsed == TYPE_VIDEO:
        return "horizontal"
    return "vertical"


def pick_save_soil_image(
    images: list[DriveMediaFile],
    *,
    orientation: VideoOrientation,
) -> DriveMediaFile | None:
    if not images:
        return None
    matching = [
        image
        for image in images
        if save_soil_image_kind(image.name) == orientation
    ]
    pool = matching or [
        image for image in images if save_soil_image_kind(image.name) is not None
    ]
    if not pool:
        pool = list(images)
    return sorted(pool, key=lambda item: item.name.casefold())[0]


def _drive_image(item: dict, *, parent_id: str) -> DriveMediaFile | None:
    file_id = item.get("id")
    name = item.get("name")
    mime_type = item.get("mimeType")
    if not isinstance(file_id, str) or not file_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        return None
    return DriveMediaFile(
        id=file_id,
        name=name,
        mime_type=mime_type,
        parent_id=parent_id,
    )


def list_save_soil_images(
    drive_service: Resource,
    folder_id: str,
    *,
    max_depth: int = 1,
) -> list[DriveMediaFile]:
    images: list[DriveMediaFile] = []

    def scan(current_id: str, depth: int) -> None:
        for item in list_folder_children(drive_service, current_id):
            resolved = resolve_drive_item(drive_service, item)
            mime_type = resolved.get("mimeType")
            resolved_id = resolved.get("id")
            if not isinstance(mime_type, str) or not isinstance(resolved_id, str):
                continue
            if mime_type.startswith("image/"):
                media = _drive_image(resolved, parent_id=current_id)
                if media is not None:
                    images.append(media)
                continue
            if mime_type == FOLDER_MIME_TYPE and depth < max_depth:
                scan(resolved_id, depth + 1)

    scan(folder_id, 0)
    return images


def find_save_soil_image(
    drive_service: Resource,
    *,
    orientation: VideoOrientation,
    folder_id: str | None = None,
) -> DriveMediaFile | None:
    target_folder = folder_id or resolve_save_soil_folder_id(drive_service)
    images = list_save_soil_images(drive_service, target_folder)
    return pick_save_soil_image(images, orientation=orientation)


def frame_looks_like_save_soil(image_path: Path) -> bool:
    """True when a still matches the dark-blue SAVE SOIL end card."""
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width < 8 or height < 8:
                return False
            pixels = rgb.load()
            return _frame_pixels_look_like_save_soil(pixels, width, height)
    except OSError:
        return False


def _frame_pixels_look_like_save_soil(pixels: object, width: int, height: int) -> bool:
    def patch_mean(left: int, top: int, right: int, bottom: int) -> tuple[float, float, float]:
        red = green = blue = count = 0
        step_x = max(1, (right - left) // 16)
        step_y = max(1, (bottom - top) // 16)
        for y in range(top, bottom, step_y):
            for x in range(left, right, step_x):
                pixel = pixels[x, y]
                red += pixel[0]
                green += pixel[1]
                blue += pixel[2]
                count += 1
        if count == 0:
            return 0.0, 0.0, 0.0
        return red / count, green / count, blue / count

    inset = max(1, min(width, height) // 40)
    corners = [
        patch_mean(0, 0, inset * 4, inset * 4),
        patch_mean(width - inset * 4, 0, width, inset * 4),
        patch_mean(0, height - inset * 4, inset * 4, height),
        patch_mean(width - inset * 4, height - inset * 4, width, height),
    ]
    corner_r = sum(item[0] for item in corners) / 4
    corner_g = sum(item[1] for item in corners) / 4
    corner_b = sum(item[2] for item in corners) / 4
    logo_r, logo_g, logo_b = patch_mean(
        width // 3,
        int(height * 0.08),
        2 * width // 3,
        int(height * 0.38),
    )
    left_r, left_g, left_b = patch_mean(
        int(width * 0.04),
        int(height * 0.15),
        int(width * 0.45),
        int(height * 0.85),
    )
    mean_r, mean_g, mean_b = patch_mean(0, 0, width, height)
    stacked_logo = logo_g >= logo_b + 10 and logo_g >= logo_r + 10
    left_logo = left_g >= left_b + 10 and left_g >= left_r + 10
    return (
        corner_b >= corner_r + 40
        and corner_b >= corner_g + 30
        and (stacked_logo or left_logo)
        and mean_b >= mean_r + 20
        and mean_b >= mean_g + 15
    )


def end_card_start_from_samples(
    samples: list[tuple[float, bool]],
    *,
    duration: float,
) -> float | None:
    """Return the first SAVE SOIL timestamp if the video ends on that card."""
    if duration < _MIN_END_CARD_SECONDS or not samples:
        return None
    ordered = sorted(samples, key=lambda item: item[0])
    if not ordered[-1][1]:
        return None
    start = ordered[-1][0]
    for timestamp, is_end_card in reversed(ordered):
        if is_end_card:
            start = timestamp
            continue
        break
    if duration - start < _MIN_END_CARD_SECONDS:
        return None
    return round(start, 2)


def _extract_frame(
    video_path: Path,
    timestamp: float,
    destination: Path,
    *,
    ffmpeg: str,
) -> Path | None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale={_FRAME_SCALE}:-2",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size < 50:
        return None
    return destination


def detect_save_soil_end_card(
    video_path: Path,
    *,
    ffmpeg_path: str | None = None,
    work_dir: Path | None = None,
) -> float | None:
    """Return the timestamp where a SAVE SOIL end card or animation begins, or None."""
    duration = probe_local_video_duration_seconds(video_path)
    if duration is None or duration < _MIN_END_CARD_SECONDS:
        return None
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    end_t = max(0.0, duration - 0.12)
    window_start = max(0.0, duration - _DETECT_WINDOW_SECONDS)
    tmp_parent: str | None = None
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        tmp_parent = str(work_dir)

    with tempfile.TemporaryDirectory(prefix="savesoil-", dir=tmp_parent) as tmp:
        tmp_dir = Path(tmp)

        def looks(timestamp: float, label: str) -> bool:
            frame = _extract_frame(
                video_path,
                timestamp,
                tmp_dir / f"{label}.jpg",
                ffmpeg=ffmpeg,
            )
            return bool(frame and frame_looks_like_save_soil(frame))

        if not looks(end_t, "end"):
            return None

        earliest = end_t
        probe = end_t
        found_content = False
        while probe - 0.5 >= window_start:
            probe -= 0.5
            if looks(probe, f"t{probe:.2f}".replace(".", "_")):
                earliest = probe
                continue
            found_content = True
            low, high = probe, earliest
            for index in range(6):
                mid = (low + high) / 2
                if looks(mid, f"m{index}"):
                    high = mid
                else:
                    low = mid
            earliest = high
            break
        if not found_content and looks(window_start, "window"):
            earliest = window_start

    return end_card_start_from_samples(
        [(earliest, True), (end_t, True)],
        duration=duration,
    )


def overlay_end_card_command(
    *,
    ffmpeg: str,
    video_path: Path,
    image_path: Path,
    output_path: Path,
    start_seconds: float,
    width: int,
    height: int,
) -> list[str]:
    even_w = width - (width % 2)
    even_h = height - (height % 2)
    enable = f"gte(t,{start_seconds:.3f})"
    filter_complex = (
        f"[1:v]scale={even_w}:{even_h}:force_original_aspect_ratio=decrease,"
        f"pad={even_w}:{even_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p[img];"
        f"[0:v][img]overlay=0:0:enable='{enable}'[vout]"
    )
    return [
        ffmpeg,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(image_path),
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def overlay_save_soil_end_card(
    video_path: Path,
    image_path: Path,
    start_seconds: float,
    output_path: Path,
    *,
    ffmpeg_path: str | None = None,
) -> Path:
    size = probe_local_video_size(video_path)
    if size is None:
        raise DriveCombineError(
            f"Could not probe video size for SAVE SOIL overlay: {video_path}"
        )
    ffmpeg = resolve_ffmpeg_path(ffmpeg_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and output_path.resolve() != video_path.resolve():
        output_path.unlink()
    command = overlay_end_card_command(
        ffmpeg=ffmpeg,
        video_path=video_path,
        image_path=image_path,
        output_path=output_path,
        start_seconds=start_seconds,
        width=size[0],
        height=size[1],
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise DriveCombineError(f"Failed to run ffmpeg SAVE SOIL overlay: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown ffmpeg error").strip()
        raise DriveCombineError(f"ffmpeg SAVE SOIL overlay failed: {detail}")
    if not output_path.exists():
        raise DriveCombineError(
            f"ffmpeg did not create SAVE SOIL overlay file: {output_path}"
        )
    return output_path


def _orientation_from_video_file(video_path: Path) -> VideoOrientation | None:
    size = probe_local_video_size(video_path)
    if size is None:
        return None
    width, height = size
    if width > height:
        return "horizontal"
    if height > width:
        return "vertical"
    return None


def replace_save_soil_end_card_if_present(
    drive_service: Resource,
    video_path: Path,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    video_type: str | None = None,
) -> Path:
    """Overlay the translated SAVE SOIL still when the video ends on that card.

    Missing image / no end card leaves ``video_path`` unchanged. Overlay ffmpeg
    failures raise so Combined Media generation does not silently keep English.
    """
    if not video_path.is_file():
        return video_path

    start = detect_save_soil_end_card(
        video_path,
        ffmpeg_path=ffmpeg_path,
        work_dir=work_dir,
    )
    if start is None:
        return video_path

    orientation = orientation_for_video_type(video_type) or _orientation_from_video_file(
        video_path
    )
    if orientation is None:
        print("SAVE SOIL end card found, but video orientation is unknown; leaving English card")
        return video_path

    try:
        image = find_save_soil_image(drive_service, orientation=orientation)
    except Exception as exc:
        print(f"SAVE SOIL image lookup failed: {exc}")
        return video_path
    if image is None:
        print(
            "SAVE SOIL end card found, but no translated image "
            f"(need SaveSoilReel.jpeg or SaveSoilVideo.jpeg) in Overrides/"
            f"{FOLDER_IMAGES!r}"
        )
        return video_path

    image_path = download_drive_file(
        drive_service,
        image.id,
        work_dir / image.name,
    )
    replaced = work_dir / f"{video_path.stem}.savesoil{video_path.suffix}"
    overlay_save_soil_end_card(
        video_path,
        image_path,
        start,
        replaced,
        ffmpeg_path=ffmpeg_path,
    )
    video_path.unlink()
    replaced.replace(video_path)
    print(
        f"Replaced SAVE SOIL end card from {start:.2f}s using {image.name!r} "
        f"({orientation})"
    )
    return video_path
