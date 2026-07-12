from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from catalog_parser.drive_media import (
    find_media_subfolder_ids,
    folder_contains_audio_and_video,
    pkg_link_has_stems_media,
)


class DriveMediaStructureTests(unittest.TestCase):
    def test_pkg_link_has_stems_media_with_nested_video(self) -> None:
        drive = MagicMock()

        def files_list(**kwargs: object) -> MagicMock:
            query = kwargs.get("q", "")
            if "pkg-folder" in query:
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "files": [
                                {
                                    "id": "media-shortcut",
                                    "name": "Any Media Folder",
                                    "mimeType": "application/vnd.google-apps.shortcut",
                                }
                            ]
                        }
                    )
                )
            if "media-folder" in query:
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
                                    "name": "ILP GLP MP4",
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

        def files_get(fileId: str, **kwargs: object) -> MagicMock:
            if fileId == "media-shortcut":
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "shortcutDetails": {
                                "targetId": "media-folder",
                                "targetMimeType": "application/vnd.google-apps.folder",
                            }
                        }
                    )
                )
            if fileId == "media-folder":
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "id": "media-folder",
                            "name": "Any Media Folder",
                            "mimeType": "application/vnd.google-apps.folder",
                        }
                    )
                )
            raise AssertionError(f"Unexpected fileId: {fileId!r}")

        drive.files.return_value.list.side_effect = files_list
        drive.files.return_value.get.side_effect = files_get

        self.assertTrue(
            pkg_link_has_stems_media(
                drive,
                "https://drive.google.com/drive/folders/pkg-folder",
            )
        )

    def test_pkg_link_missing_media_subfolder(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "video-1",
                    "name": "REF_video.mp4",
                    "mimeType": "video/mp4",
                }
            ]
        }

        self.assertFalse(
            pkg_link_has_stems_media(
                drive,
                "https://drive.google.com/drive/folders/pkg-folder",
            )
        )

    def test_find_media_subfolder_ids_accepts_any_folder_name(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "media-folder",
                    "name": "Custom Package Folder",
                    "mimeType": "application/vnd.google-apps.folder",
                }
            ]
        }

        self.assertEqual(
            find_media_subfolder_ids(drive, "pkg-folder"),
            ["media-folder"],
        )

    def test_folder_contains_audio_and_video_requires_both(self) -> None:
        drive = MagicMock()
        drive.files.return_value.list.return_value.execute.return_value = {
            "files": [
                {
                    "id": "audio-1",
                    "name": "All Dialogue.wav",
                    "mimeType": "audio/x-wav",
                }
            ]
        }

        self.assertFalse(folder_contains_audio_and_video(drive, "media-folder"))


if __name__ == "__main__":
    unittest.main()
