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

    def test_resolve_original_video_thumbnail_uses_platform_for_root_marker(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_thumbnail.pick_root_thumbnail_marker",
            return_value={"id": "tn-1", "name": "TN_sample.psd", "mimeType": "image/vnd.adobe.photoshop"},
        ):
            with patch(
                "catalog_parser.drive_thumbnail._resolve_original_platform_attachment",
                return_value=(
                    build_airtable_attachment(
                        "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
                        filename="original-youtube.jpg",
                    ),
                    "original-platform:youtube-direct:maxresdefault",
                ),
            ):
                attachment, source = resolve_original_video_thumbnail(
                    drive_service,
                    None,
                    "folder-id",
                    original_video_url="https://youtu.be/abc123",
                )

        self.assertEqual(source, "original-platform:youtube-direct:maxresdefault")
        self.assertEqual(
            attachment,
            [
                {
                    "url": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
                    "filename": "original-youtube.jpg",
                }
            ],
        )

    def test_resolve_original_video_thumbnail_uses_platform_for_canva_link(self) -> None:
        drive_service = MagicMock()
        with patch(
            "catalog_parser.drive_thumbnail.pick_root_thumbnail_marker",
            return_value=None,
        ):
            with patch(
                "catalog_parser.drive_thumbnail._discover_canva_url",
                return_value="https://canva.com/design/abc/view",
            ):
                with patch(
                    "catalog_parser.drive_thumbnail._resolve_original_platform_attachment",
                    return_value=(
                        build_airtable_attachment(
                            "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
                            filename="original-youtube.jpg",
                        ),
                        "original-platform:youtube-direct:maxresdefault",
                    ),
                ):
                    attachment, source = resolve_original_video_thumbnail(
                        drive_service,
                        None,
                        "folder-id",
                        original_video_url="https://youtu.be/abc123",
                    )

        self.assertEqual(source, "original-platform:youtube-direct:maxresdefault")
        self.assertEqual(
            attachment[0]["url"],
            "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        )

    def test_enrich_records_stages_local_thumbnail_when_staging_dir_set(self) -> None:
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
            "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
            side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
            or dest.write_bytes(b"jpg"),
        ):
            with patch(
                "catalog_parser.drive_thumbnail.has_original_video_thumbnail_source",
                return_value=True,
            ):
                enriched = enrich_records_with_original_video_thumbnails(
                    records,
                    drive_service,
                    None,
                    staging_dir=staging_dir,
                )

        self.assertIsNone(enriched[0]["ytThumbnail"])
        self.assertIn("_originalThumbnailPath", enriched[0])
        self.assertNotIn("_thumbnailReviewPath", enriched[0])
        self.assertEqual(enriched[0]["ytThumbnailSource"], "original-platform:local-upload")

    def test_enrich_records_stages_review_queue_without_tn_or_canva(self) -> None:
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
            "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
            side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
            or dest.write_bytes(b"jpg"),
        ):
            with patch(
                "catalog_parser.drive_thumbnail.has_original_video_thumbnail_source",
                return_value=False,
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
            "catalog_parser.drive_thumbnail.download_original_platform_thumbnail",
            side_effect=lambda _url, dest: dest.parent.mkdir(parents=True, exist_ok=True)
            or dest.write_bytes(b"jpg"),
        ):
            with patch(
                "catalog_parser.drive_thumbnail.has_original_video_thumbnail_source",
                return_value=False,
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
                "catalog_parser.drive_thumbnail.has_original_video_thumbnail_source",
                return_value=True,
            ):
                enriched = enrich_records_with_original_video_thumbnails(
                    [ig_record],
                    drive_service,
                    None,
                    staging_dir=staging_dir,
                    catalog_peers=peers,
                )

        self.assertIn("_originalThumbnailPath", enriched[0])
        self.assertEqual(
            enriched[0]["_originalThumbnailFallbackCtLink"],
            "https://youtu.be/abc123XYZ01",
        )
        self.assertEqual(
            enriched[0]["ytThumbnailSource"],
            "original-platform:local-upload:peer-youtube",
        )


if __name__ == "__main__":
    unittest.main()
