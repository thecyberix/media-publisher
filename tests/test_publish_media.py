import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_publisher.models import PublishJob
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.publish_media import (
    PublishMediaCleanup,
    apply_publish_media_cleanup,
    merge_publish_media_cleanup,
    resolve_publish_thumbnail,
    resolve_publish_video,
)
from media_publisher.sources.tn_publish import TnPublishSettings


class PublishMediaResolutionTests(unittest.TestCase):
    def test_merge_publish_media_cleanup(self) -> None:
        left = PublishMediaCleanup(
            drive_file_ids_to_delete=["file1"],
            canva_design_id="design1",
        )
        right = PublishMediaCleanup(
            drive_file_ids_to_delete=["file2"],
            canva_published_folder_id="published1",
        )
        merged = merge_publish_media_cleanup(left, right)
        assert merged is not None
        self.assertEqual(merged.drive_file_ids_to_delete, ["file1", "file2"])
        self.assertEqual(merged.canva_design_id, "design1")
        self.assertEqual(merged.canva_published_folder_id, "published1")

    def test_resolve_publish_video_uses_drive_override(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.return_value = DriveFile(
            id="videos-folder",
            name="Videos",
            mime_type="application/vnd.google-apps.folder",
        )
        drive.find_file_by_title.return_value = DriveFile(
            id="video123",
            name="Launch video.mp4",
            mime_type="video/mp4",
        )
        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"video")
            return dest

        drive.download_file.side_effect = _write_download

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_publish_video(
                title="Launch video",
                drive=drive,
                override_root_folder_id="root123",
                videos_subfolder="Videos",
                download_dir=Path(tmpdir),
            )

        self.assertEqual(result.source, "drive-override-video")
        assert result.path is not None
        self.assertEqual(result.cleanup.drive_file_ids_to_delete, ["video123"])
        drive.download_file.assert_called_once()

    def test_resolve_publish_thumbnail_prefers_drive_override(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.return_value = DriveFile(
            id="thumb-folder",
            name="Thumbnails",
            mime_type="application/vnd.google-apps.folder",
        )
        drive.find_file_by_title.return_value = DriveFile(
            id="thumb123",
            name="Launch video.png",
            mime_type="image/png",
        )
        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"png")
            return dest

        drive.download_file.side_effect = _write_download
        job = PublishJob(title="Translated", video_format="post")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_publish_thumbnail(
                job,
                {"Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}]},
                title="Launch video",
                canva_client=None,
                drive=drive,
                canva_download_dir=Path(tmpdir),
                long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
                short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
                override_root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                tn_settings=TnPublishSettings(
                    original_dir=Path(tmpdir) / "original",
                    cache_dir=Path(tmpdir) / "cache",
                    output_dir=Path(tmpdir) / "rendered",
                    english_override_file=Path(tmpdir) / "overrides.json",
                ),
            )

        self.assertEqual(result.source, "drive-override-thumbnail")
        assert result.path is not None
        drive.download_file.assert_called_once()

    def test_apply_publish_media_cleanup_deletes_drive_and_moves_canva(self) -> None:
        drive = MagicMock()
        canva = MagicMock()
        messages: list[str] = []
        cleanup = PublishMediaCleanup(
            drive_file_ids_to_delete=["file1"],
            canva_design_id="design1",
            canva_published_folder_id="published1",
        )
        apply_publish_media_cleanup(
            cleanup,
            drive=drive,
            canva_client=canva,
            log=messages.append,
        )
        drive.delete_file.assert_called_once_with("file1")
        canva.move_folder_item.assert_called_once_with(
            item_id="design1",
            to_folder_id="published1",
        )
        self.assertTrue(any("deleted Drive override" in message for message in messages))
        self.assertTrue(any("moved Canva design" in message for message in messages))

    def test_without_airtable_thumbnail_returns_no_thumbnail(self) -> None:
        job = PublishJob(title="Translated", video_format="short_form")
        canva = MagicMock()
        result = resolve_publish_thumbnail(
            job,
            {},
            title="Launch video",
            canva_client=canva,
            drive=None,
            canva_download_dir=Path("downloads/canva"),
            long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
            short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
            override_root_folder_id="root123",
            thumbnails_subfolder="Thumbnails",
            published_subfolder_name="Published",
            tn_settings=TnPublishSettings(
                original_dir=Path("downloads/original-thumbnails"),
                cache_dir=Path("downloads/tn-cache"),
                output_dir=Path("downloads/tn-rendered"),
                english_override_file=Path("downloads/tn-english-overrides.json"),
            ),
        )
        self.assertIsNone(result.path)
        canva.find_design_in_folder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
