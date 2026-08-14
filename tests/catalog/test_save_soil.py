from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from catalog_parser.drive_combine import DriveMediaFile
from catalog_parser.save_soil import (
    DEFAULT_SAVE_SOIL_IMAGE_DRIVE_FOLDER_ID,
    end_card_start_from_samples,
    find_save_soil_image,
    frame_looks_like_save_soil,
    orientation_for_video_type,
    overlay_end_card_command,
    pick_save_soil_image,
    replace_save_soil_end_card_if_present,
    save_soil_image_folder_id,
    save_soil_image_kind,
)


def _media(name: str, file_id: str = "id") -> DriveMediaFile:
    return DriveMediaFile(
        id=file_id,
        name=name,
        mime_type="image/jpeg",
        parent_id="folder",
    )


def _write_save_soil_still(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (18, 32, 110))
    pixels = image.load()
    for y in range(28, 110):
        for x in range(50, 130):
            if (x - 90) ** 2 + (y - 70) ** 2 <= 38**2:
                pixels[x, y] = (70, 170, 55)
    image.save(path)


def _write_horizontal_save_soil_still(path: Path) -> None:
    image = Image.new("RGB", (320, 180), (18, 32, 110))
    pixels = image.load()
    for y in range(20, 160):
        for x in range(18, 140):
            if (x - 78) ** 2 + (y - 90) ** 2 <= 52**2:
                pixels[x, y] = (70, 170, 55)
    image.save(path)


def _write_content_still(path: Path) -> None:
    image = Image.new("RGB", (180, 320), (118, 112, 118))
    pixels = image.load()
    for y in range(80, 220):
        for x in range(40, 140):
            pixels[x, y] = (160, 140, 120)
    image.save(path)


class SaveSoilImageSelectionTests(unittest.TestCase):
    def test_kind_from_canonical_names(self) -> None:
        self.assertEqual(save_soil_image_kind("SaveSoilReel.jpeg"), "vertical")
        self.assertEqual(save_soil_image_kind("SaveSoilVideo.jpeg"), "horizontal")
        self.assertIsNone(save_soil_image_kind("thumbnail.jpg"))

    def test_kind_ignores_separators_and_case(self) -> None:
        self.assertEqual(save_soil_image_kind("save_soil_reel.PNG"), "vertical")
        self.assertEqual(save_soil_image_kind("Save-Soil-Video.JPG"), "horizontal")

    def test_pick_reel_image_for_vertical(self) -> None:
        picked = pick_save_soil_image(
            [_media("SaveSoilVideo.jpeg", "video"), _media("SaveSoilReel.jpeg", "reel")],
            orientation="vertical",
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.id, "reel")

    def test_pick_video_image_for_horizontal(self) -> None:
        picked = pick_save_soil_image(
            [_media("SaveSoilReel.jpeg", "reel"), _media("SaveSoilVideo.jpeg", "video")],
            orientation="horizontal",
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.id, "video")

    def test_orientation_from_airtable_type(self) -> None:
        self.assertEqual(orientation_for_video_type("Reel"), "vertical")
        self.assertEqual(orientation_for_video_type("Short"), "vertical")
        self.assertEqual(orientation_for_video_type("Video"), "horizontal")
        self.assertIsNone(orientation_for_video_type("other"))

    def test_default_folder_id(self) -> None:
        with patch.dict("os.environ", {"SAVE_SOIL_IMAGE_DRIVE_FOLDER": ""}):
            self.assertEqual(
                save_soil_image_folder_id(),
                DEFAULT_SAVE_SOIL_IMAGE_DRIVE_FOLDER_ID,
            )

    def test_folder_id_from_env_url(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "SAVE_SOIL_IMAGE_DRIVE_FOLDER": (
                    "https://drive.google.com/drive/folders/abcDriveFolderId01"
                )
            },
        ):
            self.assertEqual(save_soil_image_folder_id(), "abcDriveFolderId01")

    def test_find_save_soil_image_picks_reel_file(self) -> None:
        drive = MagicMock()
        with patch(
            "catalog_parser.save_soil.list_folder_children",
            return_value=[
                {
                    "id": "video-img",
                    "name": "SaveSoilVideo.jpeg",
                    "mimeType": "image/jpeg",
                },
                {
                    "id": "reel-img",
                    "name": "SaveSoilReel.jpeg",
                    "mimeType": "image/jpeg",
                },
            ],
        ), patch(
            "catalog_parser.save_soil.resolve_drive_item",
            side_effect=lambda _drive, item: item,
        ):
            picked = find_save_soil_image(drive, orientation="vertical")
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.id, "reel-img")
        self.assertEqual(picked.name, "SaveSoilReel.jpeg")


class SaveSoilReplaceSkipTests(unittest.TestCase):
    def test_replace_skips_missing_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.mp4"
            result = replace_save_soil_end_card_if_present(
                MagicMock(),
                missing,
                work_dir=Path(tmpdir),
                video_type="Reel",
            )
        self.assertEqual(result, missing)


class SaveSoilDetectionTests(unittest.TestCase):
    def test_frame_fingerprint_accepts_save_soil_still(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "soil.jpg"
            _write_save_soil_still(path)
            self.assertTrue(frame_looks_like_save_soil(path))

    def test_frame_fingerprint_accepts_horizontal_save_soil_still(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "soil_video.jpg"
            _write_horizontal_save_soil_still(path)
            self.assertTrue(frame_looks_like_save_soil(path))

    def test_frame_fingerprint_rejects_content_still(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "talk.jpg"
            _write_content_still(path)
            self.assertFalse(frame_looks_like_save_soil(path))

    def test_frame_fingerprint_on_superfood_sample_frames(self) -> None:
        soil = Path("_tmp_savesoil_end_frames/t_44.jpg")
        content = Path("_tmp_savesoil_end_frames/t_42.jpg")
        if not soil.is_file() or not content.is_file():
            self.skipTest("local Superfood SAVE SOIL frames are not present")
        self.assertTrue(frame_looks_like_save_soil(soil))
        self.assertFalse(frame_looks_like_save_soil(content))

    def test_frame_fingerprint_on_animated_horizontal_end_card(self) -> None:
        start = Path("_tmp_savesoil_animated/frames/t_922.7.jpg")
        mid = Path("_tmp_savesoil_animated/frames/t_924.7.jpg")
        end = Path("_tmp_savesoil_animated/frames/t_926.7.jpg")
        content = Path("_tmp_savesoil_animated/frames/t_921.7.jpg")
        if not all(path.is_file() for path in (start, mid, end, content)):
            self.skipTest("local animated SAVE SOIL frames are not present")
        self.assertTrue(frame_looks_like_save_soil(start))
        self.assertTrue(frame_looks_like_save_soil(mid))
        self.assertTrue(frame_looks_like_save_soil(end))
        self.assertFalse(frame_looks_like_save_soil(content))

    def test_end_card_start_uses_first_matching_sample(self) -> None:
        start = end_card_start_from_samples(
            [(40.0, False), (42.5, False), (43.0, True), (47.4, True)],
            duration=47.56,
        )
        self.assertEqual(start, 43.0)

    def test_end_card_start_none_when_ending_is_content(self) -> None:
        self.assertIsNone(
            end_card_start_from_samples(
                [(10.0, False), (18.0, False)],
                duration=18.4,
            )
        )

    def test_end_card_start_none_when_card_too_short(self) -> None:
        self.assertIsNone(
            end_card_start_from_samples(
                [(9.6, False), (9.8, True)],
                duration=10.0,
            )
        )


class SaveSoilOverlayCommandTests(unittest.TestCase):
    def test_overlay_command_enables_from_detected_start(self) -> None:
        command = overlay_end_card_command(
            ffmpeg="ffmpeg",
            video_path=Path("clip.mp4"),
            image_path=Path("SaveSoilReel.jpeg"),
            output_path=Path("out.mp4"),
            start_seconds=43.0,
            width=1080,
            height=1920,
        )
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("gte(t,43.000)", " ".join(command))
        self.assertIn("SaveSoilReel.jpeg", command)
        self.assertIn("libx264", command)
        self.assertIn("0:a:0?", command)


if __name__ == "__main__":
    unittest.main()
