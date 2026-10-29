from __future__ import annotations

import ssl
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from catalog_parser.drive_combine import (
    DEFAULT_COMBINED_VIDEO_NAME,
    DriveCombineError,
    DriveMediaFile,
    _pick_audio_file,
    _pick_video_file,
    find_stems_media,
    upload_drive_file,
    verify_drive_output_folder_access,
)
from media_publisher.transient_retry import call_with_transient_retry
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


def _retry_without_sleep(fn, *args, **kwargs):
    kwargs["sleep"] = lambda _delay: None
    return call_with_transient_retry(fn, *args, **kwargs)


def _close_upload(drive: MagicMock) -> None:
    media = drive.files.return_value.create.call_args.kwargs["media_body"]
    media.stream().close()


class DriveUploadRetryTests(unittest.TestCase):
    def test_upload_retries_ssl_eof_on_the_same_request(self) -> None:
        drive = MagicMock()
        request = MagicMock()
        drive.files.return_value.create.return_value = request
        request.execute.side_effect = [
            ssl.SSLEOFError("EOF occurred in violation of protocol"),
            {"id": "file-1", "name": "clip.mp4", "mimeType": "video/mp4"},
        ]

        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"video")
            with patch(
                "catalog_parser.drive_combine.call_with_transient_retry",
                side_effect=_retry_without_sleep,
            ):
                uploaded = upload_drive_file(drive, "parent-1", source, name="clip.mp4")
            _close_upload(drive)

        self.assertEqual(uploaded.id, "file-1")
        self.assertEqual(drive.files.return_value.create.call_count, 1)
        self.assertEqual(request.execute.call_count, 2)

    def test_upload_does_not_retry_missing_folder(self) -> None:
        drive = MagicMock()
        request = MagicMock()
        drive.files.return_value.create.return_value = request
        request.execute.side_effect = HttpError(
            resp=MagicMock(status=404),
            content=b"not found",
        )

        with TemporaryDirectory() as tmp:
            source = Path(tmp) / "clip.mp4"
            source.write_bytes(b"video")
            with patch(
                "catalog_parser.drive_combine.call_with_transient_retry",
                side_effect=_retry_without_sleep,
            ):
                with self.assertRaises(DriveCombineError) as ctx:
                    upload_drive_file(drive, "missing-folder", source, name="clip.mp4")
            _close_upload(drive)

        self.assertIn("missing-folder", str(ctx.exception))
        self.assertEqual(request.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
