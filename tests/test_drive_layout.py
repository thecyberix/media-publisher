from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from media_publisher.sources.drive_layout import (
    FOLDER_COMBINED_MEDIA_FILES,
    extract_drive_folder_id,
    resolve_named_folder,
    require_drive_root_id,
)
from media_publisher.sources.google_drive import DriveFile, GoogleDriveError


class DriveLayoutTests(unittest.TestCase):
    def test_extract_drive_folder_id_from_url_and_raw_id(self) -> None:
        self.assertEqual(
            extract_drive_folder_id(
                "https://drive.google.com/drive/folders/1hJZgKn2MwztFzzd7J3rGuh4xCg3su6cg"
            ),
            "1hJZgKn2MwztFzzd7J3rGuh4xCg3su6cg",
        )
        self.assertEqual(
            extract_drive_folder_id("1hJZgKn2MwztFzzd7J3rGuh4xCg3su6cg"),
            "1hJZgKn2MwztFzzd7J3rGuh4xCg3su6cg",
        )

    def test_require_drive_root_id(self) -> None:
        self.assertEqual(
            require_drive_root_id(
                "https://drive.google.com/drive/u/1/folders/abc123"
            ),
            "abc123",
        )
        with patch.dict(os.environ, {"DRIVE_URL": ""}, clear=False):
            with self.assertRaises(GoogleDriveError):
                require_drive_root_id("")

    def test_resolve_named_folder_looks_up_child(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.return_value = DriveFile(
            id="combined-id",
            name=FOLDER_COMBINED_MEDIA_FILES,
            mime_type="application/vnd.google-apps.folder",
        )
        folder_id = resolve_named_folder(
            drive,
            FOLDER_COMBINED_MEDIA_FILES,
            drive_url="https://drive.google.com/drive/folders/parent",
        )
        self.assertEqual(folder_id, "combined-id")
        drive.find_child_folder.assert_called_once_with(
            "parent", FOLDER_COMBINED_MEDIA_FILES
        )

    def test_resolve_named_folder_missing_child(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.return_value = None
        with self.assertRaises(GoogleDriveError):
            resolve_named_folder(
                drive,
                FOLDER_COMBINED_MEDIA_FILES,
                drive_url="parent",
            )


if __name__ == "__main__":
    unittest.main()
