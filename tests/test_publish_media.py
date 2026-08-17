import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from media_publisher.models import PublishJob
from media_publisher.sources.canva import CanvaError
from media_publisher.sources.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_TITLE,
    FIELD_TRANSLATED_SUBTITLES,
)
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.publish_media import (
    CombinedMediaError,
    DriveFileMove,
    PublishMediaCleanup,
    apply_publish_media_cleanup,
    combined_media_cleanup_from_fields,
    drive_override_thumbnail_exists,
    has_prepared_publish_thumbnail,
    merge_publish_media_cleanup,
    resolve_canva_catalog_thumbnail,
    resolve_combined_media_for_publish,
    resolve_drive_override_thumbnail,
    resolve_publish_thumbnail,
    resolve_publish_video,
)
from media_publisher.sources.tn_publish import TnPublishSettings


class PublishMediaResolutionTests(unittest.TestCase):
    def test_merge_publish_media_cleanup(self) -> None:
        left = PublishMediaCleanup(
            drive_file_ids_to_delete=["file1"],
            drive_file_moves=[
                DriveFileMove(file_id="thumb1", destination_folder_id="published1")
            ],
            canva_design_id="design1",
            combined_media_file_id="combined1",
        )
        right = PublishMediaCleanup(
            drive_file_ids_to_delete=["file2"],
            canva_published_folder_id="published1",
        )
        merged = merge_publish_media_cleanup(left, right)
        assert merged is not None
        self.assertEqual(merged.drive_file_ids_to_delete, ["file1", "file2"])
        self.assertEqual(len(merged.drive_file_moves), 1)
        self.assertEqual(merged.canva_design_id, "design1")
        self.assertEqual(merged.canva_published_folder_id, "published1")
        self.assertEqual(merged.combined_media_file_id, "combined1")

    def test_combined_media_cleanup_from_fields(self) -> None:
        cleanup = combined_media_cleanup_from_fields(
            {
                FIELD_COMBINED_MEDIA_FILE: (
                    "https://drive.google.com/file/d/abc123/view"
                )
            }
        )
        assert cleanup is not None
        self.assertEqual(cleanup.combined_media_file_id, "abc123")

    def test_resolve_combined_media_downloads_existing_file(self) -> None:
        drive = MagicMock()

        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"combined")
            return dest

        drive.download_file.side_effect = _write_download

        with tempfile.TemporaryDirectory() as tmpdir:
            download_dir = Path(tmpdir) / "dl"
            result = resolve_combined_media_for_publish(
                record_fields={
                    FIELD_TITLE: "Launch video",
                    FIELD_COMBINED_MEDIA_FILE: (
                        "https://drive.google.com/file/d/file123/view"
                    ),
                    FIELD_TRANSLATED_SUBTITLES: (
                        "https://drive.google.com/file/d/subs123/view"
                    ),
                },
                drive=drive,
                download_dir=download_dir,
            )
            self.assertEqual(result.source, "combined-media")
            assert result.path is not None
            self.assertEqual(result.path.read_bytes(), b"combined")
            assert result.cleanup is not None
            self.assertEqual(result.cleanup.combined_media_file_id, "file123")
            self.assertEqual(result.cleanup.translated_subtitles_file_id, "subs123")

    def test_resolve_combined_media_requires_existing_file(self) -> None:
        drive = MagicMock()
        with self.assertRaises(CombinedMediaError):
            resolve_combined_media_for_publish(
                record_fields={FIELD_TITLE: "Launch video"},
                drive=drive,
                download_dir=Path("."),
            )
        drive.download_file.assert_not_called()

    def test_resolve_publish_video_uses_drive_override(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.return_value = DriveFile(
            id="videos-folder",
            name="Videos",
            mime_type="application/vnd.google-apps.folder",
        )
        drive.list_children.return_value = [
            DriveFile(
                id="video123",
                name="Launch video.mp4",
                mime_type="video/mp4",
            )
        ]

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

    def test_drive_override_thumbnail_moves_to_published_subfolder(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.side_effect = [
            DriveFile(
                id="thumb-folder",
                name="Thumbnails",
                mime_type="application/vnd.google-apps.folder",
            ),
            DriveFile(
                id="published-folder",
                name="Published",
                mime_type="application/vnd.google-apps.folder",
            ),
        ]
        drive.list_children.return_value = [
            DriveFile(
                id="thumb123",
                name="Launch video _ Part 2.tn-render.jpg",
                mime_type="image/jpeg",
            )
        ]

        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"jpg")
            return dest

        drive.download_file.side_effect = _write_download

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_drive_override_thumbnail(
                drive,
                root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                title="Launch video | Part 2",
                download_dir=Path(tmpdir),
            )

        self.assertEqual(result.source, "drive-override-thumbnail")
        assert result.path is not None
        assert result.cleanup is not None
        self.assertEqual(
            result.cleanup.drive_file_moves,
            [DriveFileMove(file_id="thumb123", destination_folder_id="published-folder")],
        )
        self.assertEqual(result.cleanup.drive_file_ids_to_delete, [])
        drive.download_file.assert_called_once_with("thumb123", result.path)

    def test_drive_override_thumbnail_finds_file_in_published_subfolder(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.side_effect = [
            DriveFile(
                id="thumb-folder",
                name="Thumbnails",
                mime_type="application/vnd.google-apps.folder",
            ),
            DriveFile(
                id="published-folder",
                name="Published",
                mime_type="application/vnd.google-apps.folder",
            ),
        ]

        def _list_children(folder_id: str):
            if folder_id == "thumb-folder":
                return []
            if folder_id == "published-folder":
                return [
                    DriveFile(
                        id="thumb456",
                        name="Launch video.png",
                        mime_type="image/png",
                    )
                ]
            return []

        drive.list_children.side_effect = _list_children

        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"png")
            return dest

        drive.download_file.side_effect = _write_download

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_drive_override_thumbnail(
                drive,
                root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                title="Launch video",
                download_dir=Path(tmpdir),
            )

        self.assertEqual(result.source, "drive-override-thumbnail-published")
        assert result.path is not None
        self.assertIsNone(result.cleanup)
        drive.download_file.assert_called_once_with("thumb456", result.path)

    def test_drive_override_thumbnail_prefers_active_folder_over_published(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.side_effect = [
            DriveFile(
                id="thumb-folder",
                name="Thumbnails",
                mime_type="application/vnd.google-apps.folder",
            ),
            DriveFile(
                id="published-folder",
                name="Published",
                mime_type="application/vnd.google-apps.folder",
            ),
        ]
        drive.list_children.side_effect = [
            [
                DriveFile(
                    id="thumb-active",
                    name="Launch video.png",
                    mime_type="image/png",
                )
            ],
            [
                DriveFile(
                    id="thumb-published",
                    name="Launch video.png",
                    mime_type="image/png",
                )
            ],
        ]

        def _write_download(_file_id, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"png")
            return dest

        drive.download_file.side_effect = _write_download

        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_drive_override_thumbnail(
                drive,
                root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                title="Launch video",
                download_dir=Path(tmpdir),
            )

        self.assertEqual(result.source, "drive-override-thumbnail")
        drive.download_file.assert_called_once_with("thumb-active", result.path)

    def test_resolve_publish_thumbnail_prefers_drive_override(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.side_effect = [
            DriveFile(
                id="thumb-folder",
                name="Thumbnails",
                mime_type="application/vnd.google-apps.folder",
            ),
            DriveFile(
                id="published-folder",
                name="Published",
                mime_type="application/vnd.google-apps.folder",
            ),
        ]
        drive.list_children.return_value = [
            DriveFile(
                id="thumb123",
                name="Launch video.png",
                mime_type="image/png",
            )
        ]

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

    def test_apply_publish_media_cleanup_moves_thumbnail_deletes_video_and_combined_media(
        self,
    ) -> None:
        drive = MagicMock()
        drive.remove_file.return_value = "deleted"
        airtable = MagicMock()
        messages: list[str] = []
        cleanup = PublishMediaCleanup(
            drive_file_ids_to_delete=["video1"],
            drive_file_moves=[
                DriveFileMove(file_id="thumb1", destination_folder_id="published1")
            ],
            combined_media_file_id="combined1",
            translated_subtitles_file_id="subs1",
        )
        apply_publish_media_cleanup(
            cleanup,
            drive=drive,
            canva_client=None,
            airtable=airtable,
            record_id="recABC",
            log=messages.append,
        )
        drive.move_file.assert_called_once_with("thumb1", "published1")
        drive.delete_file.assert_called_once_with("video1")
        self.assertEqual(
            drive.remove_file.call_args_list,
            [
                unittest.mock.call("combined1"),
                unittest.mock.call("subs1"),
            ],
        )
        self.assertEqual(
            airtable.update_record.call_args_list,
            [
                unittest.mock.call("recABC", {FIELD_COMBINED_MEDIA_FILE: ""}),
                unittest.mock.call("recABC", {FIELD_TRANSLATED_SUBTITLES: ""}),
            ],
        )
        self.assertTrue(any("moved Drive override file" in message for message in messages))
        self.assertTrue(any("deleted Combined Media File" in message for message in messages))
        self.assertTrue(any("deleted Translated subtitles" in message for message in messages))

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

    def test_apply_publish_media_cleanup_trashes_combined_media_when_delete_unavailable(
        self,
    ) -> None:
        drive = MagicMock()
        drive.remove_file.return_value = "trashed"
        airtable = MagicMock()
        cleanup = PublishMediaCleanup(combined_media_file_id="combined1")
        apply_publish_media_cleanup(
            cleanup,
            drive=drive,
            canva_client=None,
            airtable=airtable,
            record_id="recABC",
        )
        airtable.update_record.assert_called_once_with(
            "recABC",
            {FIELD_COMBINED_MEDIA_FILE: ""},
        )

    def test_apply_publish_media_cleanup_keeps_combined_media_when_drive_remove_fails(
        self,
    ) -> None:
        from media_publisher.sources.google_drive import GoogleDriveError

        drive = MagicMock()
        drive.remove_file.side_effect = GoogleDriveError("no permission")
        airtable = MagicMock()
        cleanup = PublishMediaCleanup(combined_media_file_id="combined1")
        apply_publish_media_cleanup(
            cleanup,
            drive=drive,
            canva_client=None,
            airtable=airtable,
            record_id="recABC",
        )
        airtable.update_record.assert_not_called()

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

    def test_resolve_publish_thumbnail_reports_all_failed_steps(self) -> None:
        from media_publisher.sources.tn_publish import TnPublishError

        job = PublishJob(title="Translated", video_format="post")
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(TnPublishError) as ctx:
                resolve_publish_thumbnail(
                    job,
                    {"Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}]},
                    title="Launch video",
                    canva_client=None,
                    drive=None,
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
        message = str(ctx.exception)
        self.assertIn("drive override: Google Drive client unavailable", message)
        self.assertIn("canva catalog: Canva client unavailable", message)
        self.assertIn("tn generation: Google Drive client unavailable", message)

    def test_resolve_publish_thumbnail_reraises_canva_auth_error(self) -> None:
        job = PublishJob(title="Translated", video_format="post")
        canva = MagicMock()
        canva.find_subfolder.return_value = None
        canva.find_design_in_folder.side_effect = CanvaError(
            "Canva token request failed with HTTP 400: invalid_grant"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(CanvaError):
                resolve_publish_thumbnail(
                    job,
                    {"Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}]},
                    title="Launch video",
                    canva_client=canva,
                    drive=MagicMock(),
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

    def test_canva_catalog_thumbnail_exists_reraises_auth_error(self) -> None:
        from media_publisher.sources.publish_media import canva_catalog_thumbnail_exists

        client = MagicMock()
        client.find_subfolder.return_value = None
        client.find_design_in_folder.side_effect = CanvaError(
            "Canva token request failed with HTTP 400: invalid_grant"
        )
        with self.assertRaises(CanvaError):
            canva_catalog_thumbnail_exists(
                client=client,
                title="Launch video",
                video_format="post",
                long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
                short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
                published_subfolder_name="Published",
            )

    def test_resolve_canva_catalog_thumbnail_finds_design_in_published_subfolder(
        self,
    ) -> None:
        from media_publisher.sources.canva import (
            CanvaDesignSummary,
            CanvaError,
            CanvaFolderSummary,
        )

        client = MagicMock()
        client.find_subfolder.return_value = CanvaFolderSummary(
            id="published-folder",
            name="Published",
        )

        def _find_design(folder_id: str, title: str) -> CanvaDesignSummary:
            if folder_id == "FAHOgLx_jAw":
                raise CanvaError("missing in catalog")
            if folder_id == "published-folder":
                return CanvaDesignSummary(id="design123", title=title)
            raise AssertionError(f"unexpected folder {folder_id}")

        client.find_design_in_folder.side_effect = _find_design

        def _download(target, destination, **kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"png")
            return destination

        client.download_thumbnail_target.side_effect = _download
        job = PublishJob(title="Translated", video_format="post")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = resolve_canva_catalog_thumbnail(
                job,
                client=client,
                title="Launch video",
                download_dir=Path(tmpdir),
                long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
                short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
                published_subfolder_name="Published",
            )

        self.assertEqual(result.source, "canva-catalog-published")
        assert result.path is not None
        self.assertIsNone(result.cleanup)
        client.find_design_in_folder.assert_any_call("published-folder", "Launch video")

    def test_drive_override_thumbnail_exists_checks_without_download(self) -> None:
        drive = MagicMock()
        drive.find_child_folder.side_effect = [
            DriveFile(id="thumbs", name="Thumbnails", mime_type="application/vnd.google-apps.folder"),
            DriveFile(id="published", name="Published", mime_type="application/vnd.google-apps.folder"),
        ]
        drive.list_children.side_effect = [
            [
                DriveFile(
                    id="img1",
                    name="Launch video.jpg",
                    mime_type="image/jpeg",
                )
            ],
            [],
        ]
        self.assertTrue(
            drive_override_thumbnail_exists(
                drive,
                root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                title="Launch video",
            )
        )
        drive.download_file.assert_not_called()

    def test_has_prepared_publish_thumbnail_true_from_canva_only(self) -> None:
        from media_publisher.sources.canva import CanvaDesignSummary, CanvaError

        canva = MagicMock()
        canva.find_subfolder.return_value = None
        canva.find_design_in_folder.return_value = CanvaDesignSummary(
            id="design1",
            title="Launch video",
        )
        self.assertTrue(
            has_prepared_publish_thumbnail(
                title="Launch video",
                video_format="post",
                drive=None,
                canva_client=canva,
                override_root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
                short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
            )
        )

        canva.find_design_in_folder.side_effect = CanvaError("missing")
        self.assertFalse(
            has_prepared_publish_thumbnail(
                title="Launch video",
                video_format="post",
                drive=None,
                canva_client=canva,
                override_root_folder_id="root123",
                thumbnails_subfolder="Thumbnails",
                published_subfolder_name="Published",
                long_catalog_url="https://www.canva.com/folder/FAHOgLx_jAw",
                short_catalog_url="https://www.canva.com/folder/FAHOgF-NT8Q",
            )
        )


if __name__ == "__main__":
    unittest.main()
