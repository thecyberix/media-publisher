from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.drive_thumbnail import (
    DriveThumbnailError,
    build_airtable_attachment,
    enrich_records_with_original_video_thumbnails,
    find_peer_youtube_ct_link,
    find_thumbnail_image_in_folder,
    resolve_canva_design_drive_url,
    resolve_original_video_thumbnail,
)


class DriveThumbnailTests(unittest.TestCase):
    def test_find_thumbnail_image_prefers_thumbnail_name(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_thumbnail.list_folder_children",
            return_value=[
                {"id": "img-1", "name": "cover.png", "mimeType": "image/png"},
                {"id": "img-2", "name": "yt_thumbnail.jpg", "mimeType": "image/jpeg"},
            ],
        ):
            with patch(
                "catalog_parser.drive_thumbnail.resolve_drive_item",
                side_effect=lambda _drive, item: item,
            ):
                image = find_thumbnail_image_in_folder(drive_service, "folder-id")

        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image["id"], "img-2")

    def test_resolve_original_video_thumbnail_ignores_drive_tn_without_canva(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value=None,
        ):
            attachment, source = resolve_original_video_thumbnail(
                drive_service,
                None,
                "folder-id",
                original_video_url="https://youtu.be/abc123",
            )
        self.assertIsNone(attachment)
        self.assertIsNone(source)

    def test_resolve_original_video_thumbnail_uses_canva_for_canva_link(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value="https://www.canva.com/design/abc/view",
        ):
            with patch(
                "catalog_parser.drive_thumbnail._resolve_canva_attachment",
                return_value=(
                    build_airtable_attachment(
                        "https://export.canva.com/thumb.jpg",
                        filename="canva-export.jpg",
                    ),
                    "canva-export",
                ),
            ) as canva_mock:
                attachment, source = resolve_original_video_thumbnail(
                    drive_service,
                    None,
                    "folder-id",
                    original_video_url="https://youtu.be/abc123",
                    canva_client=MagicMock(),
                )

        canva_mock.assert_called_once()
        self.assertEqual(source, "canva-export")
        self.assertEqual(attachment[0]["url"], "https://export.canva.com/thumb.jpg")

    def test_enrich_records_queues_review_even_when_drive_tn_present(self) -> None:
        import tempfile

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value=None,
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
                side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
                or dest.write_bytes(b"jpg"),
            ):
                with patch(
                    "catalog_parser.drive_thumbnail._original_thumbnail_matches_video_aspect",
                    return_value=True,
                ):
                    enriched = enrich_records_with_original_video_thumbnails(
                        records,
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                    )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertNotIn("_originalThumbnailPath", enriched[0])
        self.assertIn("_thumbnailReviewPath", enriched[0])
        self.assertEqual(enriched[0]["ytThumbnailSource"], "original-platform:review-queue")

    def test_enrich_records_stages_canva_thumbnail_when_canva_link(self) -> None:
        import tempfile

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value="https://www.canva.com/design/abc/view",
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_canva_thumbnail",
                side_effect=lambda _url, dest, canva_client=None: (
                    dest.parent.mkdir(parents=True, exist_ok=True),
                    dest.write_bytes(b"canva"),
                    "canva-export",
                )[-1],
            ):
                enriched = enrich_records_with_original_video_thumbnails(
                    records,
                    drive_service,
                    None,
                    staging_dir=staging_dir,
                    canva_client=MagicMock(),
                )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertIn("_originalThumbnailPath", enriched[0])
        self.assertEqual(enriched[0]["ytThumbnailSource"], "canva-export")
        self.assertNotIn("_canvaDesignUrl", enriched[0])

    def test_enrich_records_queues_manual_canva_placeholder_on_design_access_error(
        self,
    ) -> None:
        import tempfile

        from catalog_parser.canva import CanvaError

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value="https://www.canva.com/design/DAGa81rbUOw/view",
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_canva_thumbnail",
                side_effect=CanvaError(
                    'Canva POST /exports failed with HTTP 403: '
                    '{"code":"permission_denied","message":'
                    '"Not allowed to access design with id DAGa81rbUOw"}'
                ),
            ):
                with patch(
                    "catalog_parser.drive_thumbnail.video_size_from_pkg_folder",
                    return_value=(1280, 720),
                ):
                    enriched = enrich_records_with_original_video_thumbnails(
                        records,
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                        canva_client=MagicMock(),
                    )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertNotIn("_originalThumbnailPath", enriched[0])
        self.assertIn("_thumbnailReviewPath", enriched[0])
        self.assertEqual(enriched[0]["ytThumbnailSource"], "canva-manual:review-queue")
        review_path = Path(enriched[0]["_thumbnailReviewPath"])
        self.assertTrue(review_path.is_file())
        self.assertGreater(review_path.stat().st_size, 0)

    def test_enrich_records_raises_on_canva_auth_error(self) -> None:
        import tempfile

        from catalog_parser.canva import CanvaError
        from catalog_parser.drive_thumbnail import DriveThumbnailError

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value="https://www.canva.com/design/DAGa81rbUOw/view",
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_canva_thumbnail",
                side_effect=CanvaError(
                    'Canva token exchange failed with HTTP 400: '
                    '{"error":"invalid_grant","error_description":'
                    '"Token lineage has been revoked"}'
                ),
            ):
                with self.assertRaises(DriveThumbnailError):
                    enrich_records_with_original_video_thumbnails(
                        records,
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                        canva_client=MagicMock(),
                    )

    def test_enrich_records_stages_review_queue_without_canva(self) -> None:
        import tempfile

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value=None,
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
                side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
                or dest.write_bytes(b"jpg"),
            ):
                with patch(
                    "catalog_parser.drive_thumbnail._original_thumbnail_matches_video_aspect",
                    return_value=True,
                ):
                    enriched = enrich_records_with_original_video_thumbnails(
                        records,
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                    )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertNotIn("_originalThumbnailPath", enriched[0])
        self.assertIn("_thumbnailReviewPath", enriched[0])
        self.assertEqual(enriched[0]["ytThumbnailSource"], "original-platform:review-queue")

    def test_enrich_records_skips_review_when_aspect_mismatches(self) -> None:
        import tempfile

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        records = [
            {
                "ctTitle": "Sample",
                "ctLink": "https://youtu.be/abc123",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail._discover_canva_url",
            return_value=None,
        ):
            with patch(
                "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
                side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
                or dest.write_bytes(b"jpg"),
            ):
                with patch(
                    "catalog_parser.drive_thumbnail._original_thumbnail_matches_video_aspect",
                    return_value=False,
                ):
                    enriched = enrich_records_with_original_video_thumbnails(
                        records,
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                    )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertNotIn("_originalThumbnailPath", enriched[0])
        self.assertNotIn("_thumbnailReviewPath", enriched[0])
        self.assertIsNone(enriched[0]["ytThumbnailSource"])

    def test_find_peer_youtube_ct_link_same_drive_folder(self) -> None:
        peers = [
            {
                "ctTitle": "IG copy",
                "ctLink": "https://www.instagram.com/reel/abc/",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            },
            {
                "ctTitle": "YT copy",
                "ctLink": "https://youtu.be/abc123XYZ01",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            },
            {
                "ctTitle": "Other folder",
                "ctLink": "https://youtu.be/other123456",
                "pkgLink": "https://drive.google.com/drive/folders/folder-2",
            },
        ]
        found = find_peer_youtube_ct_link(
            "folder-1",
            peers,
            exclude_ct_link="https://www.instagram.com/reel/abc/",
        )
        self.assertEqual(found, "https://youtu.be/abc123XYZ01")

    def test_enrich_falls_back_to_peer_youtube_when_instagram_fails(self) -> None:
        import tempfile

        drive_service = MagicMock()
        staging_dir = Path(tempfile.mkdtemp())
        ig_record = {
            "ctTitle": "FIFA IG",
            "ctLink": "https://www.instagram.com/reel/igcode/",
            "pkgLink": "https://drive.google.com/drive/folders/folder-1",
        }
        peers = [
            ig_record,
            {
                "ctTitle": "FIFA YT",
                "ctLink": "https://youtu.be/abc123XYZ01",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            },
        ]

        def fake_download(url: str, dest: Path) -> Path:
            if "instagram" in url:
                raise DriveThumbnailError("IG fetch failed")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"jpg")
            return dest

        with patch(
            "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
            side_effect=fake_download,
        ):
            with patch(
                "catalog_parser.drive_thumbnail._discover_canva_url",
                return_value=None,
            ):
                with patch(
                    "catalog_parser.drive_thumbnail._original_thumbnail_matches_video_aspect",
                    return_value=True,
                ):
                    enriched = enrich_records_with_original_video_thumbnails(
                        [ig_record],
                        drive_service,
                        None,
                        staging_dir=staging_dir,
                        catalog_peers=peers,
                    )

        self.assertIn("_thumbnailReviewPath", enriched[0])
        self.assertNotIn("_originalThumbnailPath", enriched[0])
        self.assertEqual(
            enriched[0]["_originalThumbnailFallbackCtLink"],
            "https://youtu.be/abc123XYZ01",
        )
        self.assertEqual(
            enriched[0]["ytThumbnailSource"],
            "original-platform:review-queue:peer-youtube",
        )

    def test_resolve_canva_design_drive_url_from_folder_docs(self) -> None:
        fields = {
            "Video Folder": "https://drive.google.com/drive/folders/folder123",
            "Original Video": "https://youtu.be/abc123XYZ01",
        }
        with patch(
            "catalog_parser.drive_thumbnail._collect_canva_urls_from_folder_documents",
            return_value=["https://www.canva.com/design/DAGabc/view"],
        ) as collect_mock:
            with patch(
                "catalog_parser.drive_thumbnail.select_canva_url",
                return_value="https://www.canva.com/design/DAGabc/view",
            ) as select_mock:
                url = resolve_canva_design_drive_url(MagicMock(), fields)

        self.assertEqual(url, "https://www.canva.com/design/DAGabc/view")
        collect_mock.assert_called_once()
        select_mock.assert_called_once_with(
            ["https://www.canva.com/design/DAGabc/view"],
            original_video_url="https://youtu.be/abc123XYZ01",
        )

    def test_resolve_canva_design_drive_url_without_folder(self) -> None:
        self.assertIsNone(resolve_canva_design_drive_url(MagicMock(), {}))


if __name__ == "__main__":
    unittest.main()
