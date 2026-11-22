"""Replace English Sadhguru App end cards during Combined Media generation."""

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

SADHGURU_APP_REEL_STEM = "sadhguruappreel"
SADHGURU_APP_VIDEO_STEM = "sadhguruappvideo"
_STEM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_stem(name: str) -> str:
    return _STEM_RE.sub("", Path(name).stem.casefold())


def sadhguru_app_image_kind(name: str) -> VideoOrientation | None:
    """Return orientation encoded by SadhguruAppReel / SadhguruAppVideo."""
    stem = _normalize_stem(name)
    if stem == SADHGURU_APP_REEL_STEM or stem.startswith(SADHGURU_APP_REEL_STEM):
        return "vertical"
    if stem == SADHGURU_APP_VIDEO_STEM or stem.startswith(SADHGURU_APP_VIDEO_STEM):
        return "horizontal"
    return None


def pick_sadhguru_app_image(
    images: list[DriveMediaFile],
    *,
    orientation: VideoOrientation,
) -> DriveMediaFile | None:
    matching = [
        image for image in images if sadhguru_app_image_kind(image.name) == orientation
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


def list_sadhguru_app_images(
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
        if media is not None and sadhguru_app_image_kind(media.name) is not None:
            images.append(media)
    return images


def find_sadhguru_app_image(
    drive_service: Resource,
    *,
    orientation: VideoOrientation,
    folder_id: str | None = None,
) -> DriveMediaFile | None:
    target_folder = folder_id or resolve_save_soil_folder_id(drive_service)
    images = list_sadhguru_app_images(drive_service, target_folder)
    return pick_sadhguru_app_image(images, orientation=orientation)


def _is_yellow(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return (
        red > 170
        and green > 130
        and blue < 160
        and red > blue + 30
        and green > blue + 15
        and abs(red - green) < 90
    )


def _is_black(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red < 45 and green < 45 and blue < 45


def _samples(pixels, left: int, top: int, right: int, bottom: int):
    step_x = max(1, (right - left) // 24)
    step_y = max(1, (bottom - top) // 16)
    for y in range(top, bottom, step_y):
        for x in range(left, right, step_x):
            yield pixels[x, y]


def _fraction(pixels, left: int, top: int, right: int, bottom: int, match) -> float:
    if right <= left or bottom <= top:
        return 0.0
    samples = list(_samples(pixels, left, top, right, bottom))
    if not samples:
        return 0.0
    return sum(1 for pixel in samples if match(pixel)) / len(samples)


def _is_white(pixel: tuple[int, int, int]) -> bool:
    red, green, blue = pixel
    return red > 200 and green > 200 and blue > 200


def _looks_like_reel(pixels, width: int, height: int) -> bool:
    """Portrait card: mustard headline, store badges, phone mockup below."""
    if _fraction(pixels, 0, 0, width, max(1, int(height * 0.22)), _is_yellow) < 0.55:
        return False
    if (
        _fraction(
            pixels,
            int(width * 0.08),
            int(height * 0.30),
            max(int(width * 0.08) + 1, int(width * 0.92)),
            max(int(height * 0.30) + 1, int(height * 0.42)),
            _is_black,
        )
        < 0.10
    ):
        return False
    return (
        _fraction(
            pixels,
            int(width * 0.18),
            int(height * 0.55),
            max(int(width * 0.18) + 1, int(width * 0.82)),
            max(int(height * 0.55) + 1, int(height * 0.88)),
            _is_white,
        )
        >= 0.25
    )


def frame_looks_like_sadhguru_app(image_path: Path) -> bool:
    """True for the yellow Sadhguru App card with the two store badges.

    Horizontal cards have a mustard top, a cream lower half, and the black
    badges on the right. Vertical cards keep the mustard headline, put the
    badges under it, and show the phone mockup below. The English cards and
    the Bulgarian translations share those fields. A gold title card has
    neither the cream band nor the badges.
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

    if height > width:
        return _looks_like_reel(pixels, width, height)

    top = list(_samples(pixels, 0, 0, width, max(1, int(height * 0.40))))
    if not top or sum(1 for pixel in top if _is_yellow(pixel)) / len(top) < 0.55:
        return False

    mid_top = int(height * 0.50)
    mid_bottom = max(mid_top + 1, int(height * 0.72))
    mid = list(
        _samples(
            pixels,
            int(width * 0.15),
            mid_top,
            max(int(width * 0.15) + 1, int(width * 0.85)),
            mid_bottom,
        )
    )
    if not mid:
        return False
    red = sum(pixel[0] for pixel in mid) / len(mid)
    green = sum(pixel[1] for pixel in mid) / len(mid)
    blue = sum(pixel[2] for pixel in mid) / len(mid)
    if not (red > 180 and green > 160 and blue > 140 and green > red - 50):
        return False

    band_top = int(height * 0.74)
    band_bottom = max(band_top + 1, int(height * 0.92))
    badges = list(
        _samples(
            pixels,
            int(width * 0.50),
            band_top,
            max(int(width * 0.50) + 1, int(width * 0.78)),
            band_bottom,
        )
    )
    if not badges or sum(1 for pixel in badges if _is_black(pixel)) / len(badges) < 0.15:
        return False

    left = list(_samples(pixels, 0, band_top, max(1, int(width * 0.22)), band_bottom))
    right = list(
        _samples(
            pixels,
            int(width * 0.82),
            band_top,
            width,
            band_bottom,
        )
    )
    if left and sum(1 for pixel in left if _is_black(pixel)) / len(left) > 0.08:
        return False
    if right and sum(1 for pixel in right if _is_black(pixel)) / len(right) > 0.08:
        return False
    return True


def sadhguru_app_end_card_if_present(
    drive_service: Resource,
    video_path: Path,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    video_type: str | None = None,
    reference_path: Path | None = None,
) -> EndCardOverlay:
    """Overlay the translated Sadhguru App still when the video ends on that card.

    Detection uses ``reference_path`` when a package REF video is available,
    because the mixed file often has that card removed. The still is placed on
    ``video_path``. Missing image / no end card leaves ``video_path`` unchanged.
    ``detected`` is true when the card is found, even if the translated still
    is missing. Overlay ffmpeg failures raise so Combined Media generation
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
        frame_looks_like_sadhguru_app,
        ffmpeg_path=ffmpeg_path,
        work_dir=work_dir,
        temp_prefix="sadhguru-app-",
    )
    if start is None:
        return EndCardOverlay(video_path, False)

    orientation = orientation_for_video_type(video_type) or _orientation_from_video_file(
        video_path
    )
    if orientation is None:
        print(
            "Sadhguru App end card found, but video orientation is unknown; "
            "leaving English card"
        )
        return EndCardOverlay(video_path, True)

    try:
        image = find_sadhguru_app_image(drive_service, orientation=orientation)
    except Exception as exc:
        print(f"Sadhguru App image lookup failed: {exc}")
        return EndCardOverlay(video_path, True)
    if image is None:
        needed = (
            "SadhguruAppVideo.jpg"
            if orientation == "horizontal"
            else "SadhguruAppReel.jpg"
        )
        print(
            "Sadhguru App end card found, but no translated image "
            f"(need {needed}) in Overrides/{FOLDER_IMAGES!r}"
        )
        return EndCardOverlay(video_path, True)

    image_path = download_drive_file(
        drive_service,
        image.id,
        work_dir / image.name,
    )
    replaced = work_dir / f"{video_path.stem}.sadhguru-app{video_path.suffix}"
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
        f"Replaced Sadhguru App end card from {start:.2f}s using {image.name!r} "
        f"({orientation}, detected on {detect_on.name})"
    )
    return EndCardOverlay(video_path, True)
