from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from catalog_parser.canva_selection import select_canva_url
from catalog_parser.drive_video_size import (
    video_size_from_drive_file_metadata,
    video_size_from_pkg_folder,
)


class CanvaSelectionTests(unittest.TestCase):
    def test_select_single_url_without_probe(self) -> None:
        url = "https://www.canva.com/design/ABC123/view"
        self.assertEqual(
            select_canva_url(
                [url],
                target_size=(1920, 1080),
            ),
            "https://www.canva.com/design/ABC123",
        )

    def test_select_prefers_aspect_matching_design(self) -> None:
        landscape = "https://www.canva.com/design/LAND/view"
        portrait = "https://www.canva.com/design/PORT/view"
        with patch(
            "catalog_parser.canva_selection.probe_canva_design_dimensions",
            side_effect=lambda url: {
                "https://www.canva.com/design/LAND": (1920, 1080),
                "https://www.canva.com/design/PORT": (1080, 1920),
            }[url],
        ):
            selected = select_canva_url(
                [portrait, landscape],
                target_size=(1920, 1080),
            )
        self.assertEqual(selected, "https://www.canva.com/design/LAND")

    def test_select_prefers_drive_target_size_over_original_video_url(self) -> None:
        landscape = "https://www.canva.com/design/LAND/view"
        portrait = "https://www.canva.com/design/PORT/view"
        with patch(
            "catalog_parser.canva_selection.video_size_from_source_url",
            return_value=(1080, 1920),
        ):
            with patch(
                "catalog_parser.canva_selection.probe_canva_design_dimensions",
                side_effect=lambda url: {
                    "https://www.canva.com/design/LAND": (1920, 1080),
                    "https://www.canva.com/design/PORT": (1080, 1920),
                }[url],
            ):
                selected = select_canva_url(
                    [portrait, landscape],
                    target_size=(1920, 1080),
                    original_video_url="https://instagram.com/reel/short",
                )
        self.assertEqual(selected, "https://www.canva.com/design/LAND")


class DriveVideoSizeTests(unittest.TestCase):
    def test_video_size_from_drive_file_metadata(self) -> None:
        drive_service = MagicMock()
        drive_service.files().get().execute.return_value = {
            "videoMediaMetadata": {"width": 1920, "height": 1080},
        }
        self.assertEqual(
            video_size_from_drive_file_metadata(drive_service, "file-1"),
            (1920, 1080),
        )

    def test_video_size_from_pkg_folder(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_video_size.find_video_and_audio_subfolder",
            return_value=MagicMock(video=MagicMock(id="video-1", name="All Video.mp4")),
        ):
            with patch(
                "catalog_parser.drive_video_size.video_size_from_drive_file",
                return_value=(1080, 1920),
            ):
                self.assertEqual(
                    video_size_from_pkg_folder(drive_service, "folder-1"),
                    (1080, 1920),
                )


if __name__ == "__main__":
    unittest.main()
