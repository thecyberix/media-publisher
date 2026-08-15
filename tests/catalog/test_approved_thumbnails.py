from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from catalog_parser.workflow.approved_thumbnails import (
    process_approved_review_thumbnails_in_workflow,
)


class ApprovedThumbnailWorkflowTests(unittest.TestCase):
    def test_skips_when_service_account_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = process_approved_review_thumbnails_in_workflow(
                project_root=root,
                records=[{"id": "rec1", "fields": {"Title": "Sample"}}],
                dry_run=False,
                log=lambda *_args, **_kwargs: None,
            )
            self.assertTrue(result.skipped)
            self.assertEqual(result.processed, 0)

    @patch("media_publisher.sources.drive_layout.resolve_thumbnails_for_approval_id", return_value="folder-id")
    @patch("media_publisher.sources.thumbnail_review.process_approved_review_thumbnails")
    @patch("media_publisher.sources.google_drive.GoogleDriveClient.from_service_account")
    @patch("media_publisher.sources.airtable.AirtableClient")
    @patch("media_publisher.config.load_settings")
    def test_processes_approved_files_when_credentials_exist(
        self,
        load_settings,
        airtable_client,
        from_service_account,
        process_approved,
        _resolve_review_folder,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_account = root / "credentials" / "google-sheets-service-account.json"
            service_account.parent.mkdir(parents=True)
            service_account.write_text("{}", encoding="utf-8")

            load_settings.return_value = SimpleNamespace(
                airtable_token="token",
                airtable_base_id="base",
                airtable_table_name="table",
                google_sheets_service_account="credentials/google-sheets-service-account.json",
                drive_url="https://drive.google.com/drive/folders/parent",
                thumbnail_review_approved_subfolder="Approved",
            )
            process_approved.return_value = [
                SimpleNamespace(
                    title="Sample Video",
                    action="uploaded-approved",
                    drive_file="Sample Video.review.jpg",
                    caption_action="translated",
                    caption_detail="source=thumbnail",
                )
            ]

            result = process_approved_review_thumbnails_in_workflow(
                project_root=root,
                records=[{"id": "rec1", "fields": {"Title": "Sample Video"}}],
                dry_run=False,
                log=lambda *_args, **_kwargs: None,
            )

            self.assertFalse(result.skipped)
            self.assertEqual(result.processed, 1)
            process_approved.assert_called_once()
            self.assertTrue(process_approved.call_args.kwargs["apply"])
            self.assertEqual(process_approved.call_args.kwargs["project_root"], root)


if __name__ == "__main__":
    unittest.main()
