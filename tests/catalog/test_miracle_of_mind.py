from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from catalog_parser.inner_engineering import frame_looks_like_inner_engineering
from catalog_parser.drive_combine import DriveMediaFile
from catalog_parser.miracle_of_mind import (
    frame_looks_like_miracle_of_mind,
    miracle_of_mind_end_card_if_present,
    miracle_of_mind_image_kind,
    pick_miracle_of_mind_image,
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


def _write_starfield_card(path: Path, *, icon: tuple[int, int, int, int]) -> None:
    image = Image.new("RGB", (180, 320), (18, 22, 40))
    pixels = image.load()
    for index in range(40):
        x = (index * 37) % 180
        y = (index * 53) % 320
        pixels[x, y] = (190, 200, 220)
    left, top, right, bottom = icon
    for y in range(top, bottom):
        for x in range(left, right):
            pixels[x, y] = (200, 28, 42)
    for y in range(150, 168):
        for x in range(36, 144):
            pixels[x, y] = (230, 230, 230)
    image.save(path)


def _write_letterboxed_red_scene(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (0, 0, 0))
    pixels = image.load()
    for y in range(70, 230):
        for x in range(24, 156):
            pixels[x, y] = (168, 42, 36)
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
    for y in range(0, 28):
        for x in range(140, 180):
            pixels[x, y] = (220, 110, 20)
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

    def test_starfield_card_matches_and_corner_logo_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            card = root / "star.jpg"
            corner = root / "corner.jpg"
            _write_starfield_card(card, icon=(70, 84, 108, 122))
            _write_starfield_card(corner, icon=(148, 8, 172, 32))
            self.assertTrue(frame_looks_like_miracle_of_mind(card))
            self.assertFalse(frame_looks_like_miracle_of_mind(corner))

    def test_large_red_subject_on_black_is_not_the_app_icon(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scene = Path(tmpdir) / "scene.jpg"
            _write_letterboxed_red_scene(scene)
            self.assertFalse(frame_looks_like_miracle_of_mind(scene))

    def test_detection_uses_the_reference_and_overlays_the_still(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            video.write_bytes(b"original")
            reference = root / "REF.mp4"
            reference.write_bytes(b"reference")
            image = DriveMediaFile(
                id="img",
                name="MiracleOfMindReel.jpg",
                mime_type="image/jpeg",
                parent_id="folder",
            )

            def download(_drive, _file_id, dest: Path) -> Path:
                dest.write_bytes(b"jpeg")
                return dest

            def overlay(_video, _image, _start, replaced: Path, ffmpeg_path=None) -> Path:
                del ffmpeg_path
                replaced.write_bytes(b"replaced")
                return replaced

            with (
                patch(
                    "catalog_parser.miracle_of_mind.detect_ending_card",
                    return_value=40.5,
                ) as detect_mock,
                patch(
                    "catalog_parser.miracle_of_mind.find_miracle_of_mind_image",
                    return_value=image,
                ),
                patch(
                    "catalog_parser.miracle_of_mind.download_drive_file",
                    side_effect=download,
                ),
                patch(
                    "catalog_parser.miracle_of_mind.overlay_save_soil_end_card",
                    side_effect=overlay,
                ) as overlay_mock,
            ):
                result = miracle_of_mind_end_card_if_present(
                    object(),
                    video,
                    work_dir=root,
                    video_type="Reel",
                    reference_path=reference,
                )
            self.assertEqual(result, EndCardOverlay(video, True))
            self.assertEqual(video.read_bytes(), b"replaced")
            detect_mock.assert_called_once()
            self.assertEqual(detect_mock.call_args.args[0], reference)
            overlay_mock.assert_called_once()
            self.assertEqual(overlay_mock.call_args.args[2], 40.5)

    def test_missing_translated_still_leaves_the_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "clip.mp4"
            video.write_bytes(b"original")
            with (
                patch(
                    "catalog_parser.miracle_of_mind.detect_ending_card",
                    return_value=12.0,
                ),
                patch(
                    "catalog_parser.miracle_of_mind.find_miracle_of_mind_image",
                    return_value=None,
                ),
            ):
                result = miracle_of_mind_end_card_if_present(
                    object(),
                    video,
                    work_dir=root,
                    video_type="Reel",
                )
            self.assertEqual(result, EndCardOverlay(video, True))
            self.assertEqual(video.read_bytes(), b"original")

    def test_image_kind_follows_the_filename(self) -> None:
        self.assertEqual(miracle_of_mind_image_kind("MiracleOfMindReel.jpg"), "vertical")
        self.assertEqual(
            miracle_of_mind_image_kind("MiracleOfMindVideo.jpeg"),
            "horizontal",
        )
        self.assertIsNone(miracle_of_mind_image_kind("SaveSoilReel.jpeg"))
        reel = DriveMediaFile("reel", "MiracleOfMindReel.jpg", "image/jpeg", "folder")
        video = DriveMediaFile("video", "MiracleOfMindVideo.jpeg", "image/jpeg", "folder")
        self.assertEqual(
            pick_miracle_of_mind_image([video, reel], orientation="vertical"),
            reel,
        )
        self.assertIsNone(
            pick_miracle_of_mind_image([reel], orientation="horizontal"),
        )

    def test_missing_video_is_not_detected(self) -> None:
        result = miracle_of_mind_end_card_if_present(
            object(),
            Path("missing.mp4"),
            work_dir=Path("tmp"),
        )
        self.assertFalse(result.detected)
