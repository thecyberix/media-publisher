from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import STATUS_NOT_ASSIGNED, STATUS_TODO
from catalog_parser.workflow.ingest import (
    ingest_batch,
    ingest_batch_for_translator,
    ingest_batch_unassigned,
)


class IngestBatchTests(unittest.TestCase):
    @patch("catalog_parser.workflow.ingest.build_eligible_catalog_records")
    @patch("catalog_parser.workflow.ingest.build_canva_client_from_env", return_value=None)
    @patch("catalog_parser.workflow.ingest.get_docs_service")
    @patch("catalog_parser.workflow.ingest.get_drive_service")
    @patch("catalog_parser.workflow.ingest.get_sheets_service")
    @patch("catalog_parser.workflow.ingest.parse_catalog")
    @patch.dict(
        "os.environ",
        {"SHEET_ID": "sheet123"},
        clear=False,
    )
    def test_ingest_batch_unassigned_sets_status_without_translator(
        self,
        mock_parse_catalog: MagicMock,
        _mock_sheets: MagicMock,
        _mock_drive: MagicMock,
        _mock_docs: MagicMock,
        _mock_canva: MagicMock,
        mock_build_eligible: MagicMock,
    ) -> None:
        mock_parse_catalog.return_value = [{"ctTitle": "Example reel"}]
        mock_build_eligible.return_value = (
            [{"ctTitle": "Example reel", "_originalThumbnailPath": ""}],
            1,
        )
        airtable = MagicMock()
        airtable.list_existing_titles.return_value = set()
        airtable.create_records.return_value = ["rec123"]

        created = ingest_batch_unassigned(
            airtable,
            desired_type="Reel",
            target_count=4,
            max_video_seconds=900,
        )

        self.assertEqual(created, ["rec123"])
        created_records = airtable.create_records.call_args.args[0]
        self.assertEqual(
            created_records[0]["_airtable_fields"],
            {"Status": STATUS_NOT_ASSIGNED},
        )
        self.assertNotIn("Translator", created_records[0]["_airtable_fields"])

    @patch("catalog_parser.workflow.ingest.build_eligible_catalog_records")
    @patch("catalog_parser.workflow.ingest.build_canva_client_from_env", return_value=None)
    @patch("catalog_parser.workflow.ingest.get_docs_service")
    @patch("catalog_parser.workflow.ingest.get_drive_service")
    @patch("catalog_parser.workflow.ingest.get_sheets_service")
    @patch("catalog_parser.workflow.ingest.parse_catalog")
    @patch.dict(
        "os.environ",
        {"SHEET_ID": "sheet123"},
        clear=False,
    )
    def test_ingest_batch_for_translator_assigns_translator_and_todo(
        self,
        mock_parse_catalog: MagicMock,
        _mock_sheets: MagicMock,
        _mock_drive: MagicMock,
        _mock_docs: MagicMock,
        _mock_canva: MagicMock,
        mock_build_eligible: MagicMock,
    ) -> None:
        mock_parse_catalog.return_value = [{"ctTitle": "Example reel"}]
        mock_build_eligible.return_value = (
            [{"ctTitle": "Example reel", "_originalThumbnailPath": ""}],
            1,
        )
        airtable = MagicMock()
        airtable.list_existing_titles.return_value = set()
        airtable.create_records.return_value = ["rec456"]

        created = ingest_batch_for_translator(
            airtable,
            translator_name="Genka Petrova",
            desired_type="Reel",
            target_count=1,
            max_video_seconds=900,
        )

        self.assertEqual(created, ["rec456"])
        created_records = airtable.create_records.call_args.args[0]
        self.assertEqual(
            created_records[0]["_airtable_fields"],
            {"Translator": "Genka Petrova", "Status": STATUS_TODO},
        )

    @patch("catalog_parser.workflow.ingest.build_eligible_catalog_records")
    @patch("catalog_parser.workflow.ingest.build_canva_client_from_env", return_value=None)
    @patch("catalog_parser.workflow.ingest.get_docs_service")
    @patch("catalog_parser.workflow.ingest.get_drive_service")
    @patch("catalog_parser.workflow.ingest.get_sheets_service")
    @patch("catalog_parser.workflow.ingest.parse_catalog")
    @patch.dict(
        "os.environ",
        {"SHEET_ID": "sheet123"},
        clear=False,
    )
    def test_ingest_batch_dry_run_does_not_write(
        self,
        mock_parse_catalog: MagicMock,
        _mock_sheets: MagicMock,
        _mock_drive: MagicMock,
        _mock_docs: MagicMock,
        _mock_canva: MagicMock,
        mock_build_eligible: MagicMock,
    ) -> None:
        mock_parse_catalog.return_value = [{"ctTitle": "Example reel"}]
        mock_build_eligible.return_value = (
            [{"ctTitle": "Example reel", "_originalThumbnailPath": ""}],
            1,
        )
        airtable = MagicMock()
        airtable.list_existing_titles.return_value = set()
        logs: list[str] = []

        created = ingest_batch(
            airtable,
            desired_type="Reel",
            target_count=1,
            max_video_seconds=900,
            airtable_fields={"Status": STATUS_NOT_ASSIGNED},
            dry_run=True,
            log=logs.append,
        )

        self.assertEqual(created, [])
        airtable.create_records.assert_not_called()
        self.assertTrue(any("would ingest" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
