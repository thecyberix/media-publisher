from __future__ import annotations

import unittest

from catalog_parser.drive_video_crop import (
    CropRect,
    fixed_aspect_crop,
    looks_like_encoded_bars,
    median_crop,
    parse_cropdetect_crop,
)


class DriveVideoCropTests(unittest.TestCase):
    def test_parse_cropdetect_uses_last_crop(self) -> None:
        text = (
            "crop=640:1000:640:40\n"
            "crop=608:976:656:104\n"
        )
        crop = parse_cropdetect_crop(text)
        self.assertEqual(crop, CropRect(width=608, height=976, x=656, y=104))

    def test_india_pillarbox_looks_like_vertical_bars(self) -> None:
        content = CropRect(width=608, height=976, x=656, y=104)
        self.assertTrue(
            looks_like_encoded_bars(1920, 1080, content, "vertical")
        )
        crop = fixed_aspect_crop(1920, 1080, "vertical", content)
        self.assertEqual(crop, CropRect(width=608, height=1080, x=656, y=0))

    def test_full_frame_landscape_is_not_bars(self) -> None:
        content = CropRect(width=1920, height=1080, x=0, y=0)
        self.assertFalse(
            looks_like_encoded_bars(1920, 1080, content, "vertical")
        )

    def test_true_vertical_canvas_is_not_cropped_as_bars(self) -> None:
        content = CropRect(width=1080, height=1920, x=0, y=0)
        self.assertFalse(
            looks_like_encoded_bars(1080, 1920, content, "vertical")
        )

    def test_letterbox_looks_like_horizontal_bars(self) -> None:
        content = CropRect(width=1080, height=608, x=0, y=656)
        self.assertTrue(
            looks_like_encoded_bars(1080, 1920, content, "horizontal")
        )
        crop = fixed_aspect_crop(1080, 1920, "horizontal", content)
        self.assertEqual(crop, CropRect(width=1080, height=608, x=0, y=656))

    def test_median_crop(self) -> None:
        median = median_crop(
            [
                CropRect(width=608, height=976, x=656, y=104),
                CropRect(width=608, height=800, x=656, y=202),
                CropRect(width=608, height=976, x=656, y=102),
            ]
        )
        self.assertEqual(median.width, 608)
        self.assertEqual(median.x, 656)
