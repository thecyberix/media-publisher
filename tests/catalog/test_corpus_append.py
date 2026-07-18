from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import (
    FIELD_ORIGINAL_VIDEO_DESCRIPTION,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TIMING_EDITOR,
    FIELD_TYPE,
    FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
)
from catalog_parser.translation.corpus_append import (
    append_metadata_pairs_for_record,
    append_record_to_corpus,
)
from catalog_parser.workflow.actions import execute_action
from catalog_parser.workflow.rules import WorkflowAction, WorkflowActionType
from catalog_parser.workflow.table_cache import TableCache


class CorpusAppendTests(unittest.TestCase):
    def test_append_metadata_pairs_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metadata_path = root / "data" / "corpus" / "metadata_pairs.jsonl"
            airtable = SimpleNamespace(base_id="app1", table_name="Table")
            record = {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Hello World",
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: "Video",
                    FIELD_ORIGINAL_VIDEO_NAME: "Hello World",
                    FIELD_ORIGINAL_VIDEO_DESCRIPTION: "A description",
                    FIELD_VIDEO_NAME_TRANSLATED: "Здравей свят",
                    FIELD_VIDEO_DESCRIPTION_TRANSLATED: "Описание",
                },
            }
            count, skipped, notes = append_metadata_pairs_for_record(
                record,
                airtable=airtable,  # type: ignore[arg-type]
                project_root=root,
                metadata_path=metadata_path,
                current_year="2026",
            )
            self.assertIsNone(skipped)
            self.assertEqual(count, 2)
            self.assertEqual(notes, [])
            lines = metadata_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            kinds = {json.loads(line)["kind"] for line in lines}
            self.assertEqual(kinds, {"title", "description"})

            count2, skipped2, _ = append_metadata_pairs_for_record(
                record,
                airtable=airtable,  # type: ignore[arg-type]
                project_root=root,
                metadata_path=metadata_path,
                current_year="2026",
            )
            self.assertEqual(count2, 0)
            self.assertEqual(skipped2, "already in metadata corpus")

    def test_append_metadata_skips_when_no_bg(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            airtable = SimpleNamespace(base_id="app1", table_name="Table")
            record = {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Hello World",
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: "Video",
                    FIELD_ORIGINAL_VIDEO_NAME: "Hello World",
                },
            }
            count, skipped, _ = append_metadata_pairs_for_record(
                record,
                airtable=airtable,  # type: ignore[arg-type]
                project_root=root,
                current_year="2026",
            )
            self.assertEqual(count, 0)
            self.assertEqual(skipped, "no BG title or description")

    def test_append_record_to_corpus_skips_subtitles_without_smartcat(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            airtable = SimpleNamespace(base_id="app1", table_name="Table")
            record = {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Hello World",
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: "Video",
                    FIELD_ORIGINAL_VIDEO_NAME: "Hello World",
                    FIELD_VIDEO_NAME_TRANSLATED: "Здравей свят",
                },
            }
            result = append_record_to_corpus(
                record,
                airtable=airtable,  # type: ignore[arg-type]
                project_root=root,
            )
            self.assertEqual(result.metadata_pairs, 1)
            self.assertEqual(result.skipped_subtitles, "no Smartcat link")


class TimingEditorCorpusHookTests(unittest.TestCase):
    def test_assign_timing_editor_appends_metadata_to_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            record = {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Video A",
                    FIELD_TYPE: "Video",
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_ORIGINAL_VIDEO_NAME: "Video A",
                    FIELD_ORIGINAL_VIDEO_DESCRIPTION: "Desc",
                    FIELD_VIDEO_NAME_TRANSLATED: "Видео А",
                    FIELD_VIDEO_DESCRIPTION_TRANSLATED: "Описание",
                },
            }
            table_cache = TableCache([record])
            airtable = MagicMock()
            airtable.base_id = "app1"
            airtable.table_name = "Table"
            config = MagicMock()
            config.timing_editors = [
                SimpleNamespace(
                    name="Timing Free",
                    weekly_capacity_reels=20,
                    preferred_timing_type=None,
                )
            ]
            config.work_dir = root / "_tmp"

            action = WorkflowAction(
                action_type=WorkflowActionType.ASSIGN_TIMING_EDITOR,
                record_id="rec1",
                title="Video A",
                timing_editor_name="Timing Free",
            )
            with patch(
                "catalog_parser.translation.corpus_append.append_subtitle_pairs_for_record",
                return_value=(0, "no Smartcat link", []),
            ):
                result = execute_action(
                    action,
                    airtable=airtable,
                    config=config,
                    drive_service=None,
                    docs_service=None,
                    credentials_path=MagicMock(),
                    token_path=MagicMock(),
                    dry_run=False,
                    table_cache=table_cache,
                    project_root=root,
                )

            self.assertTrue(result.success)
            self.assertIn("Assigned timing editor 'Timing Free'", result.message)
            self.assertIn("2 metadata pair(s)", result.message)
            airtable.update_record_fields.assert_called_once_with(
                "rec1",
                {FIELD_TIMING_EDITOR: "Timing Free"},
            )
            metadata_path = root / "data" / "corpus" / "metadata_pairs.jsonl"
            self.assertTrue(metadata_path.is_file())
            self.assertEqual(len(metadata_path.read_text(encoding="utf-8").strip().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
