from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from catalog_parser.drive_combine import DriveMediaFile
from catalog_parser.inner_engineering import (
    find_inner_engineering_image,
    frame_looks_like_inner_engineering,
    inner_engineering_image_kind,
    pick_inner_engineering_image,
    replace_inner_engineering_end_card_if_present,
)
from catalog_parser.save_soil import frame_looks_like_save_soil


def _media(name: str, file_id: str = "id") -> DriveMediaFile:
    return DriveMediaFile(
        id=file_id,
        name=name,
        mime_type="image/jpeg",
        parent_id="folder",
    )


def _write_inner_engineering_still(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (40, 40, 109))
    pixels = image.load()
    for y in range(190, 310):
        for x in range(20, 160):
            pixels[x, y] = (160, 120, 100)
    for y in range(0, 28):
        for x in range(140, 180):
            pixels[x, y] = (220, 110, 20)
    image.save(path)


def _write_save_soil_still(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (18, 32, 110))
    pixels = image.load()
    for y in range(28, 110):
        for x in range(50, 130):
            if (x - 90) ** 2 + (y - 70) ** 2 <= 38**2:
                pixels[x, y] = (70, 170, 55)
    image.save(path)


def _write_blue_sky_scene(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (48, 58, 120))
    pixels = image.load()
    for y in range(150, 320):
        for x in range(0, 180):
            pixels[x, y] = (176, 154, 132)
    image.save(path)


def _write_blue_stage(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (24, 28, 70))
    pixels = image.load()
    for y in range(120, 300):
        for x in range(40, 150):
            pixels[x, y] = (150, 120, 90)
    image.save(path)


def _write_content_still(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (118, 112, 118))
    pixels = image.load()
    for y in range(80, 220):
        for x in range(40, 140):
            pixels[x, y] = (160, 140, 120)
    image.save(path)


class InnerEngineeringImageSelectionTests(unittest.TestCase):
    def test_kind_from_canonical_names(self) -> None:
        self.assertEqual(inner_engineering_image_kind("InnerEngineeringReel.jpg"), "vertical")
        self.assertEqual(
            inner_engineering_image_kind("InnerEngineeringVideo.jpeg"),
            "horizontal",
        )
        self.assertIsNone(inner_engineering_image_kind("SaveSoilReel.jpeg"))

    def test_pick_ignores_save_soil_stills(self) -> None:
        picked = pick_inner_engineering_image(
            [
                _media("SaveSoilReel.jpeg", "soil"),
                _media("InnerEngineeringReel.jpg", "ie"),
            ],
            orientation="vertical",
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.id, "ie")

    def test_pick_requires_matching_orientation(self) -> None:
        self.assertIsNone(
            pick_inner_engineering_image(
                [_media("InnerEngineeringReel.jpg", "ie")],
                orientation="horizontal",
            )
        )

    def test_find_image_uses_reel_file(self) -> None:
        drive = MagicMock()
        with patch(
            "catalog_parser.inner_engineering.list_folder_children",
            return_value=[
                {
                    "id": "soil",
                    "name": "SaveSoilReel.jpeg",
                    "mimeType": "image/jpeg",
                },
                {
                    "id": "ie",
                    "name": "InnerEngineeringReel.jpg",
                    "mimeType": "image/jpeg",
                },
            ],
        ), patch(
            "catalog_parser.inner_engineering.resolve_drive_item",
            side_effect=lambda _drive, item: item,
        ):
            picked = find_inner_engineering_image(
                drive, orientation="vertical", folder_id="images"
            )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.name, "InnerEngineeringReel.jpg")


class InnerEngineeringDetectionTests(unittest.TestCase):
    def test_frame_fingerprint_accepts_inner_engineering_still(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "ie.jpg"
            _write_inner_engineering_still(path)
            self.assertTrue(frame_looks_like_inner_engineering(path))
            self.assertFalse(frame_looks_like_save_soil(path))

    def test_frame_fingerprint_rejects_save_soil_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            soil = Path(tmpdir) / "soil.jpg"
            content = Path(tmpdir) / "talk.jpg"
            _write_save_soil_still(soil)
            _write_content_still(content)
            self.assertFalse(frame_looks_like_inner_engineering(soil))
            self.assertFalse(frame_looks_like_inner_engineering(content))

    def test_frame_fingerprint_rejects_blue_scenes_with_a_warm_subject(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sky = root / "sky.jpg"
            stage = root / "stage.jpg"
            _write_blue_sky_scene(sky)
            _write_blue_stage(stage)
            self.assertFalse(frame_looks_like_inner_engineering(sky))
            self.assertFalse(frame_looks_like_inner_engineering(stage))

    def test_frame_fingerprint_on_example_end_card(self) -> None:
        card = Path("_tmp_ie/last.png")
        partial = Path("_tmp_ie/frames/end_012.jpg")
        content = Path("_tmp_ie/frames/end_001.jpg")
        if not all(path.is_file() for path in (card, partial, content)):
            self.skipTest("local Inner Engineering frames are not present")
        self.assertTrue(frame_looks_like_inner_engineering(card))
        self.assertTrue(frame_looks_like_inner_engineering(partial))
        self.assertFalse(frame_looks_like_inner_engineering(content))
        reference = Path("_tmp_ie/ref_end.jpg")
        if reference.is_file():
            self.assertTrue(frame_looks_like_inner_engineering(reference))


class InnerEngineeringReplaceSkipTests(unittest.TestCase):
    def test_detects_on_reference_when_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            video = root / "mixed.mp4"
            reference = root / "REF 720p.mp4"
            video.write_bytes(b"video")
            reference.write_bytes(b"ref")
            with patch(
                "catalog_parser.inner_engineering.detect_ending_card",
                return_value=None,
            ) as detect:
                replace_inner_engineering_end_card_if_present(
                    MagicMock(),
                    video,
                    work_dir=root,
                    video_type="Reel",
                    reference_path=reference,
                )
        detect.assert_called_once()
        self.assertEqual(detect.call_args.args[0], reference)

    def test_replace_skips_missing_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.mp4"
            result = replace_inner_engineering_end_card_if_present(
                MagicMock(),
                missing,
                work_dir=Path(tmpdir),
                video_type="Reel",
            )
        self.assertEqual(result.video_path, missing)
        self.assertFalse(result.detected)


if __name__ == "__main__":
    unittest.main()
