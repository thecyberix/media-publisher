from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.drive_combine import DriveCombineError, DriveMediaFile
from catalog_parser.drive_media import FOLDER_MIME_TYPE
from catalog_parser.drive_mix import (
    _pick_merge_video,
    _sanitize_local_filename,
    check_mixable_media,
    find_video_and_audio_subfolder,
    format_mix_media_check,
    mix_folder_media_to_drive,
)


class DriveMixStructureTests(unittest.TestCase):
    def _media(self, name: str, *, file_id: str = "file") -> DriveMediaFile:
        return DriveMediaFile(
            id=file_id,
            name=name,
            mime_type="video/mp4",
            parent_id="folder",
        )

    def test_pick_merge_video_prefers_all_video_over_ocd(self) -> None:
        picked = _pick_merge_video(
            [
                self._media("OCD-31759.mp4", file_id="ocd"),
                self._media("All Video.mp4", file_id="all"),
            ]
        )
        self.assertEqual(picked.name, "All Video.mp4")

    def test_pick_merge_video_prefers_reel_all_video_when_multiple(self) -> None:
        picked = _pick_merge_video(
            [
                self._media("Copy of YT_Ask-your-burning-questions-EOE - All Video.mp4", file_id="yt"),
                self._media("Copy of REEL_Ask your burning questions-EOE - All Video.mp4", file_id="reel"),
                self._media("Copy of FB_Ask your burning questions-EOE - All Video.mp4", file_id="fb"),
            ]
        )
        self.assertEqual(picked.id, "reel")

    def test_pick_merge_video_selects_horizontal_for_video_type(self) -> None:
        drive = MagicMock()
        sizes = {
            "yt": (1920, 1080),
            "reel": (1080, 1920),
            "fb": (1920, 1080),
        }

        def size_for(_service: object, file_id: str) -> tuple[int, int] | None:
            return sizes.get(file_id)

        with patch(
            "catalog_parser.drive_video_size.video_size_from_drive_file_metadata",
            side_effect=size_for,
        ):
            picked = _pick_merge_video(
                [
                    self._media(
                        "Copy of YT_Ask-your-burning-questions-EOE - All Video.mp4",
                        file_id="yt",
                    ),
                    self._media(
                        "Copy of REEL_Ask your burning questions-EOE - All Video.mp4",
                        file_id="reel",
                    ),
                    self._media(
                        "Copy of FB_Ask your burning questions-EOE - All Video.mp4",
                        file_id="fb",
                    ),
                ],
                drive_service=drive,
                required_orientation="horizontal",
            )
        self.assertEqual(picked.id, "fb")

    def test_pick_merge_video_selects_vertical_for_reel_type(self) -> None:
        drive = MagicMock()
        sizes = {
            "yt": (1920, 1080),
            "reel": (1080, 1920),
            "fb": (1920, 1080),
        }

        def size_for(_service: object, file_id: str) -> tuple[int, int] | None:
            return sizes.get(file_id)

        with patch(
            "catalog_parser.drive_video_size.video_size_from_drive_file_metadata",
            side_effect=size_for,
        ):
            picked = _pick_merge_video(
                [
                    self._media(
                        "Copy of YT_Ask-your-burning-questions-EOE - All Video.mp4",
                        file_id="yt",
                    ),
                    self._media(
                        "Copy of REEL_Ask your burning questions-EOE - All Video.mp4",
                        file_id="reel",
                    ),
                    self._media(
                        "Copy of FB_Ask your burning questions-EOE - All Video.mp4",
                        file_id="fb",
                    ),
                ],
                drive_service=drive,
                required_orientation="vertical",
            )
        self.assertEqual(picked.id, "reel")

    def test_pick_merge_video_rejects_wrong_orientation_only(self) -> None:
        drive = MagicMock()

        with patch(
            "catalog_parser.drive_video_size.video_size_from_drive_file_metadata",
            return_value=(1080, 1920),
        ):
            with self.assertRaises(DriveCombineError) as ctx:
                _pick_merge_video(
                    [self._media("All Video.mp4", file_id="vertical")],
                    drive_service=drive,
                    required_orientation="horizontal",
                )
        self.assertIn("horizontal", str(ctx.exception))

    def test_pick_merge_video_uses_non_ref_non_ocd_fallback(self) -> None:
        picked = _pick_merge_video(
            [
                self._media("All Titles.mp4", file_id="titles"),
                self._media("OCD-30435.mp4", file_id="ocd"),
            ]
        )
        self.assertEqual(picked.name, "All Titles.mp4")

    def test_pick_merge_video_rejects_only_ref_and_ocd(self) -> None:
        with self.assertRaises(DriveCombineError):
            _pick_merge_video(
                [
                    self._media("REF_There Is No Such Thing As A Bad Day In The Existence.mp4"),
                    self._media("OCD-31731.mp4"),
                ]
            )

    def test_find_video_and_audio_subfolder_requires_single_folder(self) -> None:
        drive = MagicMock()

        def files_list(**kwargs: object) -> MagicMock:
            query = kwargs.get("q", "")
            if "pkg-folder" in query:
                return MagicMock(
                    execute=MagicMock(
                        return_value={
                            "files": [
                                {
                                    "id": "audios-1",
                                    "name": "Audios",
                                    "mimeType": "application/vnd.google-apps.folder",
                                },
                                {
                                    "id": "audios-2",
                                    "name": "Other",
                                    "mimeType": "application/vnd.google-apps.folder",
                                },
                            ]
                        }
                    )
                )
            raise AssertionError(f"Unexpected query: {query!r}")

        drive.files.return_value.list.side_effect = files_list

        with self.assertRaises(Exception):
            find_video_and_audio_subfolder(drive, "pkg-folder")

    def test_check_mixable_media_success(self) -> None:
        drive = MagicMock()
        folder_children = {
            "pkg-folder": [
                {
                    "id": "audio-folder",
                    "name": "Stems",
                    "mimeType": FOLDER_MIME_TYPE,
                },
                {
                    "id": "ref-video",
                    "name": "REF_clip.mp4",
                    "mimeType": "video/mp4",
                },
            ],
            "audio-folder": [
                {
                    "id": "video-file",
                    "name": "All Video.mp4",
                    "mimeType": "video/mp4",
                },
                {
                    "id": "a1",
                    "name": "dialogue.wav",
                    "mimeType": "audio/wav",
                },
            ],
        }

        def list_children(_service: object, folder_id: str) -> list[dict]:
            return folder_children[folder_id]

        with patch(
            "catalog_parser.drive_mix.list_folder_children",
            side_effect=list_children,
        ):
            with patch(
                "catalog_parser.drive_mix.resolve_drive_item",
                side_effect=lambda _service, item: item,
            ):
                check = check_mixable_media(drive, "pkg-folder")

        self.assertTrue(check.ok)
        self.assertIsNotNone(check.media)
        assert check.media is not None
        self.assertEqual(check.media.video.name, "All Video.mp4")
        self.assertEqual([audio.name for audio in check.media.audios], ["dialogue.wav"])
        self.assertIn("All Video.mp4", format_mix_media_check(check))

    def test_check_mixable_media_reports_missing_video(self) -> None:
        drive = MagicMock()
        folder_children = {
            "pkg-folder": [
                {
                    "id": "audio-folder",
                    "name": "Stems",
                    "mimeType": FOLDER_MIME_TYPE,
                },
                {
                    "id": "ref-video",
                    "name": "REF_clip.mp4",
                    "mimeType": "video/mp4",
                },
            ],
            "audio-folder": [
                {
                    "id": "a1",
                    "name": "dialogue.wav",
                    "mimeType": "audio/wav",
                },
                {
                    "id": "ocd-video",
                    "name": "OCD-31731.mp4",
                    "mimeType": "video/mp4",
                },
            ],
        }

        def list_children(_service: object, folder_id: str) -> list[dict]:
            return folder_children[folder_id]

        with patch(
            "catalog_parser.drive_mix.list_folder_children",
            side_effect=list_children,
        ):
            with patch(
                "catalog_parser.drive_mix.resolve_drive_item",
                side_effect=lambda _service, item: item,
            ):
                check = check_mixable_media(drive, "pkg-folder")

        self.assertFalse(check.ok)
        self.assertIn("No suitable video file found for merge", check.error or "")

    def test_mix_folder_media_to_drive_dry_run_validates_structure(self) -> None:
        drive = MagicMock()
        folder_children = {
            "pkg-folder": [
                {
                    "id": "audio-folder",
                    "name": "Audio",
                    "mimeType": FOLDER_MIME_TYPE,
                },
            ],
            "audio-folder": [],
        }

        def list_children(_service: object, folder_id: str) -> list[dict]:
            return folder_children[folder_id]

        with patch(
            "catalog_parser.drive_mix.list_folder_children",
            side_effect=list_children,
        ):
            with patch(
                "catalog_parser.drive_mix.resolve_drive_item",
                side_effect=lambda _service, item: item,
            ):
                with self.assertRaises(DriveCombineError):
                    mix_folder_media_to_drive(
                        drive,
                        pkg_folder_id="pkg-folder",
                        output_parent_id="output-folder",
                        output_name="combined.mp4",
                        work_dir=MagicMock(),
                        dry_run=True,
                    )

    def test_sanitize_local_filename_replaces_windows_invalid_characters(self) -> None:
        self.assertEqual(
            _sanitize_local_filename('REF_The Simplest Constipation Remedy | Sadhguru.mp4'),
            "REF_The Simplest Constipation Remedy _ Sadhguru.mp4",
        )

    def test_mix_folder_media_to_drive_sanitizes_temp_paths(self) -> None:
        drive = MagicMock()
        media = MagicMock()
        media.video = DriveMediaFile(
            id="video-id",
            name="video | name.mp4",
            mime_type="video/mp4",
            parent_id="pkg-folder",
        )
        media.audios = [
            DriveMediaFile(
                id="audio-id",
                name="audio:track.wav",
                mime_type="audio/wav",
                parent_id="audio-folder",
            )
        ]

        with patch(
            "catalog_parser.drive_mix.find_video_and_audio_subfolder",
            return_value=media,
        ):
            with patch("catalog_parser.drive_mix.download_drive_file") as download_mock:
                download_mock.side_effect = lambda _drive, _id, path: path
                with patch("catalog_parser.drive_mix.combine_video_with_mixed_audios") as combine_mock:
                    with patch("catalog_parser.drive_mix.upload_drive_file") as upload_mock:
                        upload_mock.return_value = MagicMock(
                            id="uploaded-id",
                            name="output | file.mp4",
                            mime_type="video/mp4",
                            parent_id="output-folder",
                        )
                        mix_folder_media_to_drive(
                            drive,
                            pkg_folder_id="pkg-folder",
                            output_parent_id="output-folder",
                            output_name="output | file.mp4",
                            work_dir=Path("tmp"),
                            dry_run=False,
                        )

        download_paths = [call.args[2] for call in download_mock.call_args_list]
        self.assertTrue(str(download_paths[0]).endswith("video _ name.mp4"))
        self.assertTrue(str(download_paths[1]).endswith("audio_track.wav"))
        output_path = combine_mock.call_args.args[2]
        self.assertTrue(str(output_path).endswith("output _ file.mp4"))


if __name__ == "__main__":
    unittest.main()
