from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from catalog_parser.drive_combine import (
    DEFAULT_COMBINED_VIDEO_NAME,
    DriveCombineError,
    DriveMediaFile,
    _pick_audio_file,
    _pick_video_file,
    find_stems_media,
    verify_drive_output_folder_access,
)
from googleapiclient.errors import HttpError


class DriveCombineSelectionTests(unittest.TestCase):
    def test_pick_audio_prefers_all_dialogue(self) -> None:
        files = [
            DriveMediaFile("1", "All Music.wav", "audio/x-wav", "parent"),
            DriveMediaFile("2", "All Dialogue.wav", "audio/x-wav", "parent"),
        ]
        picked = _pick_audio_file(files)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.name, "All Dialogue.wav")

    def test_pick_video_prefers_all_video(self) -> None:
        files = [
            DriveMediaFile("1", "OCD-30392.mp4", "video/mp4", "parent"),
            DriveMediaFile("2", "All Video.mp4", "video/mp4", "parent"),
        ]
        picked = _pick_video_file(files)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.name, "All Video.mp4")


class DriveCombineFindTests(unittest.TestCase):
    def test_verify_drive_output_folder_access_reports_service_account_hint(self) -> None:
        drive = MagicMock()
        response = MagicMock(status=404)
        drive.files.return_value.get.return_value.execute.side_effect = HttpError(
            resp=response,
            content=b"not found",
        )

        with self.assertRaises(DriveCombineError) as ctx:
            verify_drive_output_folder_access(drive, "output-folder")

        self.assertIn("output-folder", str(ctx.exception))
        self.assertIn("Share that folder", str(ctx.exception))

    def test_find_stems_media_matches_nested_structure(self) -> None:
        drive = MagicMock()

        def files_list(**kwargs: object) -> MagicMock:
            query = kwargs.get("q", "")
            if "pkg-folder" in query:
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "files": [
                                {
                                    "id": "stems-folder",
                                    "name": "Stems",
                                    "mimeType": "application/vnd.google-apps.folder",
                                }
                            ]
                        }
                    )
                )
            if "stems-folder" in query:
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "files": [
                                {
                                    "id": "audio-1",
                                    "name": "All Dialogue.wav",
                                    "mimeType": "audio/x-wav",
                                },
                                {
                                    "id": "video-folder",
                                    "name": "ILP and GLP Mp4",
                                    "mimeType": "application/vnd.google-apps.folder",
                                },
                            ]
                        }
                    )
                )
            if "video-folder" in query:
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "files": [
                                {
                                    "id": "video-1",
                                    "name": "All Video.mp4",
                                    "mimeType": "video/mp4",
                                }
                            ]
                        }
                    )
                )
            raise AssertionError(f"Unexpected query: {query!r}")

        drive.files.return_value.list.side_effect = files_list

        stems = find_stems_media(
            drive,
            "https://drive.google.com/drive/folders/pkg-folder",
        )
        self.assertEqual(stems.media_folder_id, "stems-folder")
        self.assertEqual(stems.audio.name, "All Dialogue.wav")
        self.assertEqual(stems.video.name, "All Video.mp4")
        self.assertEqual(stems.output_name, DEFAULT_COMBINED_VIDEO_NAME)


if __name__ == "__main__":
    unittest.main()
