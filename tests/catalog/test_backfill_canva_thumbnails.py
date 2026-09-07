from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    STATUS_TODO,
)
from catalog_parser.workflow.backfill_canva_thumbnails import backfill_canva_thumbnails


class BackfillCanvaThumbnailsTests(unittest.TestCase):
    def test_dry_run_reports_would_upload(self) -> None:
        airtable = MagicMock()
        airtable.list_records.return_value = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    FIELD_STATUS: STATUS_TODO,
                    FIELD_TYPE: "Reel",
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc123",
                    FIELD_ORIGINAL_VIDEO: "https://youtu.be/x",
                },
            }
        ]
        with patch(
            "catalog_parser.workflow.backfill_canva_thumbnails.discover_package_canva_url",
            return_value="https://www.canva.com/design/DAGabc",
        ):
            result = backfill_canva_thumbnails(
                airtable=airtable,
                drive_service=MagicMock(),
                docs_service=MagicMock(),
                canva_client=MagicMock(),
                dry_run=True,
                log=lambda _msg: None,
            )
        self.assertEqual(result.with_canva, 1)
        self.assertEqual(result.would_upload, 1)
        self.assertEqual(result.captions_would_translate, 1)
        self.assertEqual(result.uploaded, 0)
        self.assertEqual(result.items[0].caption_action, "would_translate")
        airtable.upload_attachment.assert_not_called()

    def test_apply_uploads_and_translates_missing_caption(self) -> None:
        airtable = MagicMock()
        airtable.list_records.return_value = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    FIELD_STATUS: STATUS_TODO,
                    FIELD_TYPE: "Reel",
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc123",
                    "Original Video Thumbnail": [{"url": "https://example.com/old.jpg"}],
                },
            }
        ]
        with TemporaryDirectory() as tmp:
            with patch(
                "catalog_parser.workflow.backfill_canva_thumbnails.discover_package_canva_url",
                return_value="https://www.canva.com/design/DAGabc",
            ), patch(
                "catalog_parser.workflow.backfill_canva_thumbnails.download_canva_thumbnail",
                side_effect=lambda _url, destination, canva_client=None: (
                    Path(destination).write_bytes(b"jpg") or "canva-export"
                ),
            ), patch(
                "catalog_parser.workflow.backfill_canva_thumbnails._translate_caption_if_missing",
                return_value=("translated", "source=thumbnail"),
            ) as caption_mock:
                result = backfill_canva_thumbnails(
                    airtable=airtable,
                    drive_service=MagicMock(),
                    docs_service=MagicMock(),
                    canva_client=MagicMock(),
                    dry_run=False,
                    project_root=Path(tmp),
                    log=lambda _msg: None,
                )
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(result.captions_translated, 1)
        self.assertTrue(result.items[0].had_thumbnail)
        self.assertFalse(result.items[0].had_caption)
        self.assertEqual(result.items[0].caption_action, "translated")
        self.assertEqual(len(result.modified_items), 1)
        airtable.upload_attachment.assert_called_once()
        self.assertEqual(
            airtable.upload_attachment.call_args.kwargs.get("replace"),
            True,
        )
        caption_mock.assert_called_once()

    def test_apply_skips_caption_when_already_set(self) -> None:
        airtable = MagicMock()
        airtable.list_records.return_value = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    FIELD_STATUS: STATUS_TODO,
                    FIELD_TYPE: "Reel",
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc123",
                    FIELD_VIDEO_CAPTION_TRANSLATED: "Вече преведен",
                },
            }
        ]
        with patch(
            "catalog_parser.workflow.backfill_canva_thumbnails.discover_package_canva_url",
            return_value="https://www.canva.com/design/DAGabc",
        ), patch(
            "catalog_parser.workflow.backfill_canva_thumbnails.download_canva_thumbnail",
            side_effect=lambda _url, destination, canva_client=None: (
                Path(destination).write_bytes(b"jpg") or "canva-export"
            ),
        ), patch(
            "catalog_parser.workflow.backfill_canva_thumbnails._translate_caption_if_missing",
            return_value=("skipped", "caption already set"),
        ):
            result = backfill_canva_thumbnails(
                airtable=airtable,
                drive_service=MagicMock(),
                docs_service=MagicMock(),
                canva_client=MagicMock(),
                dry_run=False,
                log=lambda _msg: None,
            )
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(result.captions_translated, 0)
        self.assertTrue(result.items[0].had_caption)
        self.assertEqual(result.items[0].caption_action, "skipped")


if __name__ == "__main__":
    unittest.main()
