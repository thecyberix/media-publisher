from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from catalog_parser.inner_engineering import frame_looks_like_inner_engineering
from catalog_parser.miracle_of_mind import (
    frame_looks_like_miracle_of_mind,
    miracle_of_mind_end_card_if_present,
)
from catalog_parser.save_soil import EndCardOverlay, frame_looks_like_save_soil


def _write_app_icon(path: Path, *, size: tuple[int, int], box: tuple[int, int, int, int]) -> None:
    image = Image.new("RGB", size, (2, 2, 8))
    pixels = image.load()
    left, top, right, bottom = box
    for y in range(top, bottom):
        for x in range(left, right):
            pixels[x, y] = (150, 40, 70)
    image.save(path)


def _write_talking_head(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (20, 36, 78))
    pixels = image.load()
    for y in range(40, 300):
        for x in range(30, 160):
            pixels[x, y] = (168, 140, 142)
    image.save(path)


def _write_save_soil(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (18, 32, 110))
    pixels = image.load()
    for y in range(28, 110):
        for x in range(50, 130):
            if (x - 90) ** 2 + (y - 70) ** 2 <= 38**2:
                pixels[x, y] = (70, 170, 55)
    image.save(path)


def _write_inner_engineering(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (40, 40, 109))
    pixels = image.load()
    for y in range(190, 310):
        for x in range(20, 160):
            pixels[x, y] = (160, 120, 100)
    image.save(path)


class MiracleOfMindFingerprintTests(unittest.TestCase):
    def test_vertical_and_horizontal_app_icons_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            vertical = root / "vertical.jpg"
            horizontal = root / "horizontal.jpg"
            _write_app_icon(vertical, size=(180, 320), box=(58, 70, 122, 150))
            _write_app_icon(horizontal, size=(320, 180), box=(110, 55, 210, 125))
            self.assertTrue(frame_looks_like_miracle_of_mind(vertical))
            self.assertTrue(frame_looks_like_miracle_of_mind(horizontal))
            self.assertFalse(frame_looks_like_save_soil(vertical))
            self.assertFalse(frame_looks_like_inner_engineering(vertical))

    def test_talking_head_save_soil_and_inner_engineering_do_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talking = root / "talk.jpg"
            soil = root / "soil.jpg"
            inner = root / "inner.jpg"
            _write_talking_head(talking)
            _write_save_soil(soil)
            _write_inner_engineering(inner)
            self.assertFalse(frame_looks_like_miracle_of_mind(talking))
            self.assertFalse(frame_looks_like_miracle_of_mind(soil))
            self.assertFalse(frame_looks_like_miracle_of_mind(inner))
            self.assertTrue(frame_looks_like_save_soil(soil))
            self.assertTrue(frame_looks_like_inner_engineering(inner))

    def test_detection_does_not_replace_the_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            video.write_bytes(b"original")
            reference = root / "REF.mp4"
            reference.write_bytes(b"reference")
            with patch(
                "catalog_parser.miracle_of_mind.detect_ending_card",
                return_value=40.5,
            ) as detect_mock:
                result = miracle_of_mind_end_card_if_present(
                    object(),
                    video,
                    work_dir=root,
                    video_type="Reel",
                    reference_path=reference,
                )
            self.assertEqual(result, EndCardOverlay(video, True))
            self.assertEqual(video.read_bytes(), b"original")
            detect_mock.assert_called_once()
            self.assertEqual(detect_mock.call_args.args[0], reference)

    def test_missing_video_is_not_detected(self) -> None:
        result = miracle_of_mind_end_card_if_present(
            object(),
            Path("missing.mp4"),
            work_dir=Path("tmp"),
        )
        self.assertFalse(result.detected)
