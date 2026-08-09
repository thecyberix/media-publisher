from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_TITLE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
)
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.thumbnail_review import (
    format_review_email,
    process_approved_review_thumbnails,
    review_drive_filename,
    sanitize_review_stem,
    title_from_review_filename,
    thumbnail_matches_reference_aspect,
)
from PIL import Image


class ThumbnailReviewTests(unittest.TestCase):
    def test_review_filename_roundtrip(self) -> None:
        title = "Sample | Video"
        filename = review_drive_filename(title)
        self.assertTrue(filename.endswith(".review.jpg"))
        stem = title_from_review_filename(filename)
        self.assertEqual(stem, sanitize_review_stem(title))

    def test_title_from_review_filename_rejects_non_review(self) -> None:
        self.assertIsNone(title_from_review_filename("sample.jpg"))

    def test_thumbnail_matches_reference_aspect(self) -> None:
        image = Image.new("RGB", (1920, 1080))
        self.assertTrue(
            thumbnail_matches_reference_aspect(
                image,
                reference_width=1280,
                reference_height=720,
            )
        )
        portrait = Image.new("RGB", (1080, 1920))
        self.assertFalse(
            thumbnail_matches_reference_aspect(
                portrait,
                reference_width=1280,
                reference_height=720,
            )
        )

    def test_format_review_email(self) -> None:
        from media_publisher.sources.thumbnail_review import ReviewQueueItem

        subject, body = format_review_email(
            [
                ReviewQueueItem(
                    record_id="rec1",
                    title="Sample Video",
                    local_path=Path("sample.review.jpg"),
                    reason="different background",
                )
            ]
        )
        self.assertIn("1 video", subject)
        self.assertIn("Sample Video", body)
        self.assertIn("Approved", body)

    def test_process_approved_translates_empty_caption(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.drive_service = object()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]

        def download(_file_id: str, dest: Path) -> None:
            dest.write_bytes(b"fake-image")

        drive.download_file.side_effect = download
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc",
            },
        )

        def fake_translate(catalog_record, **_kwargs):
            catalog_record["bgCaption"] = "Превод"
            from catalog_parser.translation.caption_prefill import CaptionTranslateResult

            return CaptionTranslateResult(
                ok=True,
                caption_translated=True,
                source="thumbnail",
            )

        with patch(
            "catalog_parser.translation.caption_prefill.translate_record_caption_if_needed",
            side_effect=fake_translate,
        ) as translate:
            results = process_approved_review_thumbnails(
                airtable,
                drive,
                [record],
                review_folder_id="review-id",
                apply=True,
                project_root=Path("."),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "uploaded-approved")
        self.assertEqual(results[0].caption_action, "translated")
        airtable.upload_attachment.assert_called_once()
        airtable.update_record.assert_called_once_with(
            "rec1",
            {FIELD_VIDEO_CAPTION_TRANSLATED: "Превод"},
        )
        translate.assert_called_once()
        drive.remove_file.assert_called_once_with("file1")

    def test_process_approved_skips_caption_when_already_set(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.drive_service = object()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]
        drive.download_file.side_effect = lambda _fid, dest: dest.write_bytes(b"x")
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_VIDEO_CAPTION_TRANSLATED: "Вече преведено",
            },
        )

        with patch(
            "catalog_parser.translation.caption_prefill.translate_record_caption_if_needed"
        ) as translate:
            results = process_approved_review_thumbnails(
                airtable,
                drive,
                [record],
                review_folder_id="review-id",
                apply=True,
            )

        self.assertEqual(results[0].caption_action, "skipped")
        self.assertEqual(results[0].caption_detail, "caption already set")
        translate.assert_not_called()
        airtable.update_record.assert_not_called()
        airtable.upload_attachment.assert_called_once()
        args = airtable.upload_attachment.call_args
        self.assertEqual(args.args[0], "rec1")
        self.assertEqual(args.args[1], FIELD_ORIGINAL_VIDEO_THUMBNAIL)

    def test_process_approved_dry_run_skips_caption(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]
        record = SimpleNamespace(id="rec1", fields={FIELD_TITLE: "Sample Video"})

        with patch(
            "catalog_parser.translation.caption_prefill.translate_record_caption_if_needed"
        ) as translate:
            results = process_approved_review_thumbnails(
                airtable,
                drive,
                [record],
                review_folder_id="review-id",
                apply=False,
            )

        self.assertEqual(results[0].action, "planned-approved")
        self.assertEqual(results[0].caption_action, "skipped")
        self.assertEqual(results[0].caption_detail, "dry-run")
        translate.assert_not_called()
        airtable.upload_attachment.assert_not_called()
        drive.download_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
