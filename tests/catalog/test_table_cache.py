from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from catalog_parser.airtable import (
    FIELD_EDITOR,
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
)
from catalog_parser.workflow.table_cache import TableCache


class TableCacheTests(unittest.TestCase):
    def test_existing_titles_and_update_fields(self) -> None:
        cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Video A",
                        FIELD_STATUS: "1. To do",
                        FIELD_TYPE: "Video",
                    },
                }
            ]
        )

        self.assertEqual(cache.existing_titles(), {"video a"})
        cache.update_fields("rec1", {FIELD_EDITOR: "Nina Rueva"})
        self.assertEqual(cache.get("rec1")["fields"][FIELD_EDITOR], "Nina Rueva")

    def test_existing_original_video_names(self) -> None:
        cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Poison title",
                        FIELD_ORIGINAL_VIDEO_NAME: (
                            "You’re Misunderstanding Karma Completely"
                        ),
                    },
                }
            ]
        )
        self.assertEqual(
            cache.existing_original_video_names(),
            {"you're misunderstanding karma completely"},
        )

    def test_existing_original_video_keys(self) -> None:
        cache = TableCache(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Let Life Become A Dance",
                        FIELD_ORIGINAL_VIDEO: (
                            "https://www.instagram.com/p/DXwoY7rzBzu"
                        ),
                    },
                }
            ]
        )
        self.assertEqual(
            cache.existing_original_video_keys(),
            {"ig:DXwoY7rzBzu"},
        )

    def test_register_created_from_catalog(self) -> None:
        cache = TableCache([])
        cache.register_created_from_catalog(
            [
                {
                    "ctTitle": "New Reel",
                    "ctDuration": 45,
                    "ctLink": "https://example.com/video",
                    "pkgLink": "https://drive.google.com/folders/abc",
                    "_airtable_fields": {
                        "Translator": "Genka Petrova",
                        "Status": "1. To do",
                    },
                }
            ],
            ["recNEW"],
        )

        record = cache.get("recNEW")
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["fields"][FIELD_TITLE], "New Reel")
        self.assertEqual(record["fields"]["Translator"], "Genka Petrova")
        self.assertIn("new reel", cache.existing_titles())

    def test_write_backup(self) -> None:
        fetched_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        cache = TableCache(
            [{"id": "rec1", "fields": {FIELD_TITLE: "Video A"}}],
            fetched_at=fetched_at,
        )

        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            path = cache.write_backup(project_root)
            self.assertTrue(path.exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["record_count"], 1)
            self.assertEqual(payload["records"][0]["id"], "rec1")
            latest = project_root / "output" / "backups" / "airtable-latest.json"
            self.assertTrue(latest.exists())

    def test_from_backup_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "backup.json"
            path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-07-10T12:00:00+00:00",
                        "record_count": 1,
                        "records": [{"id": "rec1", "fields": {FIELD_TITLE: "Video A"}}],
                    }
                ),
                encoding="utf-8",
            )
            cache = TableCache.from_backup_file(path)
            self.assertEqual(len(cache.records), 1)
            self.assertEqual(cache.backup_metadata["record_count"], 1)

    def test_load_fetches_once_and_writes_backup(self) -> None:
        airtable = MagicMock()
        airtable.list_records.return_value = [
            {"id": "rec1", "fields": {FIELD_TITLE: "Video A"}},
        ]

        with tempfile.TemporaryDirectory() as tmp:
            cache = TableCache.load(
                airtable,
                project_root=Path(tmp),
            )

        self.assertEqual(len(cache.records), 1)
        airtable.list_records.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
