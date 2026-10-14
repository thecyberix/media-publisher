"""Replace English Miracle of Mind end cards during Combined Media generation."""

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
from media_publisher.sources.drive_layout import FOLDER_IMAGES, resolve_save_soil_folder_id
from media_publisher.video_duration import probe_local_video_size

MIRACLE_OF_MIND_REEL_STEM = "miracleofmindreel"
MIRACLE_OF_MIND_VIDEO_STEM = "miracleofmindvideo"
_STEM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_stem(name: str) -> str:
    return _STEM_RE.sub("", Path(name).stem.casefold())


def miracle_of_mind_image_kind(name: str) -> VideoOrientation | None:
    """Return orientation encoded by MiracleOfMindReel / MiracleOfMindVideo."""
    stem = _normalize_stem(name)
    if stem == MIRACLE_OF_MIND_REEL_STEM or stem.startswith(MIRACLE_OF_MIND_REEL_STEM):
        return "vertical"
    if stem == MIRACLE_OF_MIND_VIDEO_STEM or stem.startswith(MIRACLE_OF_MIND_VIDEO_STEM):
        return "horizontal"
    return None


def pick_miracle_of_mind_image(
    images: list[DriveMediaFile],
    *,
    orientation: VideoOrientation,
) -> DriveMediaFile | None:
    matching = [
        image for image in images if miracle_of_mind_image_kind(image.name) == orientation
    ]
    if not matching:
        return None
    return sorted(matching, key=lambda item: item.name.casefold())[0]


def _centered_starfield_icon(pixels, width: int, height: int) -> bool:
    """True for the dark star-field card with one centered app icon.

    The icon is a small solid red square. A corner watermark is smaller and
    sits at the edge. A red subject (food, clothing, a backdrop) is much larger.
    """
    hot_x: list[int] = []
    hot_y: list[int] = []
    dark = 0
    for y in range(height):
        for x in range(width):
            red, green, blue = pixels[x, y]
            if (red + green + blue) / 3 < 50:
                dark += 1
            if red >= 160 and green <= 80 and red >= green + 70 and red >= blue + 50:
                hot_x.append(x)
                hot_y.append(y)
    sampled = width * height
    if dark / sampled < 0.90:
        return False
    count = len(hot_x)
    fraction = count / sampled
    if fraction < 0.010 or fraction > 0.030:
        return False
    left, right = min(hot_x), max(hot_x)
    top, bottom = min(hot_y), max(hot_y)
    box_w = right - left + 1
    box_h = bottom - top + 1
    if box_w < 8 or box_h < 8:
        return False
    aspect = box_w / box_h
    if aspect < 0.75 or aspect > 1.35:
        return False
    if box_w / width > 0.34 or box_h / height > 0.22:
        return False
    if count / (box_w * box_h) < 0.55:
        return False
    center_x = (left + right) / 2 / width
    center_y = (top + bottom) / 2 / height
    if center_x < 0.38 or center_x > 0.62 or left / width < 0.28 or right / width > 0.72:
        return False
    if center_y < 0.18 or center_y > 0.50:
        return False
    return True


def frame_looks_like_miracle_of_mind(image_path: Path) -> bool:
    """True for a Miracle of Mind end card.

    Matches the near-black slate and the dark star-field card. Both carry the
    red app icon. A large red subject does not match.
    """
    try:
        with Image.open(image_path) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            if width < 8 or height < 8:
                return False
            pixels = rgb.load()
    except OSError:
        return False

    if _centered_starfield_icon(pixels, width, height):
        return True

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

    field_r, field_g, field_b = patch_mean(0, 0, width, height)
    # The app icon is small. A large red subject (food, clothing, a backdrop)
    # lifts the frame's average red well above the real slate.
    if field_r > 28 or (field_r + field_g + field_b) / 3 > 24:
        return False
    near_black = sampled = 0
    step_x = max(1, width // 48)
    step_y = max(1, height // 48)
    for y in range(0, height, step_y):
        for x in range(0, width, step_x):
            red, green, blue = pixels[x, y]
            sampled += 1
            if red < 28 and green < 28 and blue < 36:
                near_black += 1
    if sampled == 0 or near_black / sampled < 0.76:
        return False

    inset = max(1, min(width, height) // 12)
    corners = [
        patch_mean(0, 0, inset, inset),
        patch_mean(width - inset, 0, width, inset),
        patch_mean(0, height - inset, inset, height),
        patch_mean(width - inset, height - inset, width, height),
    ]
    corner_r = sum(item[0] for item in corners) / 4
    corner_g = sum(item[1] for item in corners) / 4
    corner_b = sum(item[2] for item in corners) / 4
    if corner_r > 18 or corner_g > 18 or corner_b > 16:
        return False

    windows = (
        (0.28, 0.15, 0.72, 0.50),
        (0.28, 0.28, 0.72, 0.68),
        (0.32, 0.18, 0.68, 0.46),
    )
    for left, top, right, bottom in windows:
        red, green, blue = patch_mean(
            int(width * left),
            int(height * top),
            max(int(width * left) + 1, int(width * right)),
            max(int(height * top) + 1, int(height * bottom)),
        )
        if red >= 90 and green <= 100 and red >= green + 35 and red >= blue + 15:
            return True
    return False


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


def list_miracle_of_mind_images(
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
        if media is not None and miracle_of_mind_image_kind(media.name) is not None:
            images.append(media)
    return images


def find_miracle_of_mind_image(
    drive_service: Resource,
    *,
    orientation: VideoOrientation,
    folder_id: str | None = None,
) -> DriveMediaFile | None:
    target_folder = folder_id or resolve_save_soil_folder_id(drive_service)
    images = list_miracle_of_mind_images(drive_service, target_folder)
    return pick_miracle_of_mind_image(images, orientation=orientation)


def miracle_of_mind_end_card_if_present(
    drive_service: Resource,
    video_path: Path,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    video_type: str | None = None,
    reference_path: Path | None = None,
) -> EndCardOverlay:
    """Overlay the translated Miracle of Mind still when the video ends on that card.

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
        frame_looks_like_miracle_of_mind,
        ffmpeg_path=ffmpeg_path,
        work_dir=work_dir,
        temp_prefix="miracle-",
    )
    if start is None:
        return EndCardOverlay(video_path, False)

    orientation = orientation_for_video_type(video_type) or _orientation_from_video_file(
        video_path
    )
    if orientation is None:
        print(
            "Miracle of Mind end card found, but video orientation is unknown; "
            "leaving English card"
        )
        return EndCardOverlay(video_path, True)

    try:
        image = find_miracle_of_mind_image(drive_service, orientation=orientation)
    except Exception as exc:
        print(f"Miracle of Mind image lookup failed: {exc}")
        return EndCardOverlay(video_path, True)
    if image is None:
        needed = (
            "MiracleOfMindVideo.jpeg"
            if orientation == "horizontal"
            else "MiracleOfMindReel.jpg"
        )
        print(
            "Miracle of Mind end card found, but no translated image "
            f"(need {needed}) in Overrides/{FOLDER_IMAGES!r}"
        )
        return EndCardOverlay(video_path, True)

    image_path = download_drive_file(
        drive_service,
        image.id,
        work_dir / image.name,
    )
    replaced = work_dir / f"{video_path.stem}.miracle{video_path.suffix}"
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
        f"Replaced Miracle of Mind end card from {start:.2f}s using {image.name!r} "
        f"({orientation}, detected on {detect_on.name})"
    )
    return EndCardOverlay(video_path, True)
