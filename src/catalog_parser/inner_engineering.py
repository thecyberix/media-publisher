"""Replace English Inner Engineering end cards during Combined Media generation."""

from __future__ import annotations

import re
from pathlib import Path

from googleapiclient.discovery import Resource
from PIL import Image

from catalog_parser.drive_combine import DriveMediaFile, download_drive_file
from catalog_parser.drive_media import (
    FOLDER_MIME_TYPE,
    list_folder_children,
    resolve_drive_item,
)
from catalog_parser.save_soil import (
    EndCardOverlay,
    VideoOrientation,
    detect_ending_card,
    orientation_for_video_type,
    overlay_save_soil_end_card,
)
from media_publisher.sources.drive_layout import (
    FOLDER_IMAGES,
    resolve_save_soil_folder_id,
)
from media_publisher.video_duration import probe_local_video_size

INNER_ENGINEERING_REEL_STEM = "innerengineeringreel"
INNER_ENGINEERING_VIDEO_STEM = "innerengineeringvideo"
_STEM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_stem(name: str) -> str:
    return _STEM_RE.sub("", Path(name).stem.casefold())


def inner_engineering_image_kind(name: str) -> VideoOrientation | None:
    """Return orientation encoded by InnerEngineeringReel / InnerEngineeringVideo."""
    stem = _normalize_stem(name)
    if stem == INNER_ENGINEERING_REEL_STEM or stem.startswith(INNER_ENGINEERING_REEL_STEM):
        return "vertical"
    if stem == INNER_ENGINEERING_VIDEO_STEM or stem.startswith(INNER_ENGINEERING_VIDEO_STEM):
        return "horizontal"
    return None


def pick_inner_engineering_image(
    images: list[DriveMediaFile],
    *,
    orientation: VideoOrientation,
) -> DriveMediaFile | None:
    matching = [
        image
        for image in images
        if inner_engineering_image_kind(image.name) == orientation
    ]
    if not matching:
        return None
    return sorted(matching, key=lambda item: item.name.casefold())[0]


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


def list_inner_engineering_images(
    drive_service: Resource,
    folder_id: str,
) -> list[DriveMediaFile]:
    images: list[DriveMediaFile] = []
    for item in list_folder_children(drive_service, folder_id):
        resolved = resolve_drive_item(drive_service, item)
        mime_type = resolved.get("mimeType")
        if not isinstance(mime_type, str) or mime_type == FOLDER_MIME_TYPE:
            continue
        if not mime_type.startswith("image/"):
            continue
        media = _drive_image(resolved, parent_id=folder_id)
        if media is not None and inner_engineering_image_kind(media.name) is not None:
            images.append(media)
    return images


def find_inner_engineering_image(
    drive_service: Resource,
    *,
    orientation: VideoOrientation,
    folder_id: str | None = None,
) -> DriveMediaFile | None:
    target_folder = folder_id or resolve_save_soil_folder_id(drive_service)
    images = list_inner_engineering_images(drive_service, target_folder)
    return pick_inner_engineering_image(images, orientation=orientation)


def _is_navy(red: float, green: float, blue: float) -> bool:
    return (
        blue >= red + 35
        and blue >= green + 30
        and red < 100
        and green < 110
        and 45 <= blue <= 170
    )


def _is_navy_field(red: float, green: float, blue: float) -> bool:
    """Navy plate with white and orange type averaged in."""
    return (
        blue >= red + 20
        and blue >= green + 15
        and red < 150
        and green < 150
        and 55 <= blue <= 180
    )


def _is_warm_portrait(red: float, green: float, blue: float) -> bool:
    return red > blue + 20 and red > green and red > 110


def frame_looks_like_inner_engineering(image_path: Path) -> bool:
    """True for the dark-blue Inner Engineering card with the portrait below."""
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width < 8 or height < 8:
                return False
            pixels = rgb.load()
    except OSError:
        return False

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

    top_left = patch_mean(0, 0, max(1, width // 5), max(1, height // 8))
    upper = patch_mean(width // 4, height // 10, 3 * width // 4, height // 3)
    lower = patch_mean(width // 5, 3 * height // 5, 4 * width // 5, 9 * height // 10)
    return (
        _is_navy(*top_left)
        and _is_navy_field(*upper)
        and _is_warm_portrait(*lower)
    )


def replace_inner_engineering_end_card_if_present(
    drive_service: Resource,
    video_path: Path,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    video_type: str | None = None,
    reference_path: Path | None = None,
) -> EndCardOverlay:
    """Overlay the translated Inner Engineering still when the video ends on that card.

    Detection uses ``reference_path`` when a package REF video is available,
    because the mixed file often has that card removed. The still is placed on
    ``video_path``. Missing image / no end card leaves ``video_path`` unchanged.
    ``detected`` is true when the English card is found, even if the translated
    still is missing. Overlay ffmpeg failures raise so Combined Media generation
    does not silently keep English.
    """
    if not video_path.is_file():
        return EndCardOverlay(video_path, False)

    detect_on = (
        reference_path
        if reference_path is not None and reference_path.is_file()
        else video_path
    )
    start = detect_ending_card(
        detect_on,
        frame_looks_like_inner_engineering,
        ffmpeg_path=ffmpeg_path,
        work_dir=work_dir,
        temp_prefix="innereng-",
    )
    if start is None:
        return EndCardOverlay(video_path, False)

    orientation = orientation_for_video_type(video_type) or _orientation_from_video_file(
        video_path
    )
    if orientation is None:
        print(
            "Inner Engineering end card found, but video orientation is unknown; "
            "leaving English card"
        )
        return EndCardOverlay(video_path, True)

    try:
        image = find_inner_engineering_image(drive_service, orientation=orientation)
    except Exception as exc:
        print(f"Inner Engineering image lookup failed: {exc}")
        return EndCardOverlay(video_path, True)
    if image is None:
        needed = (
            "InnerEngineeringVideo.jpeg"
            if orientation == "horizontal"
            else "InnerEngineeringReel.jpg"
        )
        print(
            "Inner Engineering end card found, but no translated image "
            f"(need {needed}) in Overrides/{FOLDER_IMAGES!r}"
        )
        return EndCardOverlay(video_path, True)

    image_path = download_drive_file(
        drive_service,
        image.id,
        work_dir / image.name,
    )
    replaced = work_dir / f"{video_path.stem}.innereng{video_path.suffix}"
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
        f"Replaced Inner Engineering end card from {start:.2f}s using {image.name!r} "
        f"({orientation}, detected on {detect_on.name})"
    )
    return EndCardOverlay(video_path, True)
