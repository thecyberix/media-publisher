from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from catalog_parser.workflow.approved_thumbnails import (
    process_approved_review_thumbnails_in_workflow,
    process_pending_review_thumbnails_in_workflow,
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

    @patch("media_publisher.sources.drive_layout.resolve_thumbnails_for_approval_id", return_value="folder-id")
    @patch("media_publisher.sources.thumbnail_review.process_pending_review_thumbnails")
    @patch("media_publisher.sources.google_drive.GoogleDriveClient.from_service_account")
    @patch("media_publisher.sources.airtable.AirtableClient")
    @patch("catalog_parser.translation.prefill.ai_prefill_enabled", return_value=True)
    @patch("media_publisher.config.load_settings")
    def test_auto_sorts_pending_review_files(
        self,
        load_settings,
        _ai_enabled,
        airtable_client,
        from_service_account,
        process_pending,
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
            process_pending.return_value = [
                SimpleNamespace(
                    drive_file="Titled.review.jpg",
                    decision="approve",
                    action="uploaded-approved",
                    reason="has title",
                    caption_action="translated",
                    caption_detail="source=thumbnail",
                )
            ]
            result = process_pending_review_thumbnails_in_workflow(
                project_root=root,
                records=[{"id": "rec1", "fields": {"Title": "Titled"}}],
                dry_run=False,
                log=lambda *_args, **_kwargs: None,
            )
            self.assertEqual(result.sorted_count, 1)
            self.assertEqual(result.approved, 1)
            process_pending.assert_called_once()
            self.assertTrue(process_pending.call_args.kwargs["apply"])
            self.assertEqual(
                process_pending.call_args.kwargs["project_root"],
                root,
            )
            self.assertIsNotNone(process_pending.call_args.kwargs["airtable"])
            self.assertEqual(len(process_pending.call_args.kwargs["records"]), 1)
            self.assertNotIn("rejected_subfolder", process_pending.call_args.kwargs)
            airtable_client.assert_called_once()
            from_service_account.assert_called_once()

    @patch("catalog_parser.translation.prefill.ai_prefill_enabled", return_value=False)
    @patch("media_publisher.config.load_settings")
    def test_auto_sort_skips_when_ai_disabled(self, load_settings, _ai_enabled) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service_account = root / "credentials" / "google-sheets-service-account.json"
            service_account.parent.mkdir(parents=True)
            service_account.write_text("{}", encoding="utf-8")
            load_settings.return_value = SimpleNamespace(
                google_sheets_service_account="credentials/google-sheets-service-account.json",
            )
            result = process_pending_review_thumbnails_in_workflow(
                project_root=root,
                records=[],
                dry_run=False,
                log=lambda *_args, **_kwargs: None,
            )
            self.assertTrue(result.skipped_run)
            self.assertEqual(result.sorted_count, 0)


if __name__ == "__main__":
    unittest.main()
