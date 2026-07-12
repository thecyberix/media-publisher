from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from catalog_parser.drive_thumbnail import (
    build_airtable_attachment,
    enrich_records_with_original_video_thumbnails,
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

    def test_enrich_records_without_staging_dir_uses_url_attachment(self) -> None:
        drive_service = MagicMock()
        records = [
            {
                "ctTitle": "Sample",
                "pkgLink": "https://drive.google.com/drive/folders/folder-1",
            }
        ]
        with patch(
            "catalog_parser.drive_thumbnail.resolve_original_video_thumbnail",
            return_value=(
                build_airtable_attachment("https://cdn.example/thumb.jpg"),
                "original-platform:youtube-direct:maxresdefault",
            ),
        ):
            enriched = enrich_records_with_original_video_thumbnails(
                records,
                drive_service,
                None,
            )

        self.assertEqual(
            enriched[0]["ytThumbnail"],
            [{"url": "https://cdn.example/thumb.jpg"}],
        )
        self.assertEqual(
            enriched[0]["ytThumbnailSource"],
            "original-platform:youtube-direct:maxresdefault",
        )


if __name__ == "__main__":
    unittest.main()
