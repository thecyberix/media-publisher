"""Detect the Miracle of Mind end animation during Combined Media generation.

The animation stays in the video. A match only marks the video for the
configured playlist.
"""

from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import Resource
from PIL import Image

from catalog_parser.save_soil import EndCardOverlay, detect_ending_card


def frame_looks_like_miracle_of_mind(image_path: Path) -> bool:
    """True for the near-black end slate with the red Miracle of Mind app icon."""
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


def miracle_of_mind_end_card_if_present(
    drive_service: Resource,
    video_path: Path,
    *,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    video_type: str | None = None,
    reference_path: Path | None = None,
) -> EndCardOverlay:
    """Return whether the reference video ends on the Miracle of Mind animation.

    Detection uses ``reference_path`` when a package REF video is available.
    The video file is left unchanged.
    """
    del drive_service, video_type
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
    return EndCardOverlay(video_path, start is not None)
