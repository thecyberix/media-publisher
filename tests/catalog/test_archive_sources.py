from __future__ import annotations

import unittest
from unittest.mock import patch

from catalog_parser.airtable import (
    AirtableArchiveSource,
    AirtableClient,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_STATUS,
    FIELD_TITLE,
)
from catalog_parser.workflow.archive_sources import (
    STATUS_NOT_ASSIGNED,
    archive_pointers_from_records,
    parse_archive_pointer_title,
    resolve_archive_sources,
)


class ArchiveSourceDiscoveryTests(unittest.TestCase):
    def test_parse_archive_pointer_title(self) -> None:
        parsed = parse_archive_pointer_title(
            "2024 archive: https://airtable.com/invite/l?inviteId=abc"
        )
        self.assertEqual(
            parsed,
            ("2024", "https://airtable.com/invite/l?inviteId=abc"),
        )

    def test_archive_pointers_from_records_ignores_non_archive_rows(self) -> None:
        records = [
            {
                "fields": {
                    FIELD_STATUS: STATUS_NOT_ASSIGNED,
                    FIELD_TITLE: "2025 archive: https://airtable.com/invite/l?x=1",
                }
            },
            {
                "fields": {
                    FIELD_STATUS: "1. To do",
                    FIELD_TITLE: "Regular video title",
                }
            },
        ]

        self.assertEqual(
            archive_pointers_from_records(records),
            [("2025", "https://airtable.com/invite/l?x=1")],
        )

    def test_resolve_archive_sources_from_not_assigned_rows(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Translator's Paradise")
        records = [
            {
                "fields": {
                    FIELD_STATUS: STATUS_NOT_ASSIGNED,
                    FIELD_TITLE: "2024 archive: https://airtable.com/invite/l?x=2024",
                }
            },
            {
                "fields": {
                    FIELD_STATUS: STATUS_NOT_ASSIGNED,
                    FIELD_TITLE: "2025 archive: https://airtable.com/invite/l?x=2025",
                }
            },
        ]

        with patch.object(
            client,
            "list_accessible_bases",
            return_value=[
                {"id": "app2024", "name": "Archive 2024"},
                {"id": "app2025", "name": "Archive 2025"},
            ],
        ):
            with patch.object(
                client,
                "list_base_tables",
                side_effect=[
                    [
                        {
                            "name": "Table 2",
                            "fields": [{"name": FIELD_ORIGINAL_VIDEO_NAME}],
                        }
                    ],
                    [
                        {
                            "name": "Translator's Paradise",
                            "fields": [
                                {"name": FIELD_ORIGINAL_VIDEO_NAME},
                                {"name": FIELD_TITLE},
                            ],
                        }
                    ],
                ],
            ) as tables_mock:
                sources = resolve_archive_sources(client, records=records)

        self.assertEqual(
            sources,
            [
                AirtableArchiveSource(
                    base_id="app2024",
                    table_name="Table 2",
                    title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
                ),
                AirtableArchiveSource(
                    base_id="app2025",
                    table_name="Translator's Paradise",
                    title_fields=(FIELD_TITLE, FIELD_ORIGINAL_VIDEO_NAME),
                ),
            ],
        )
        self.assertEqual(tables_mock.call_count, 2)

    def test_resolve_archive_sources_returns_empty_without_archive_rows(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Translator's Paradise")
        self.assertEqual(resolve_archive_sources(client, records=[]), [])


if __name__ == "__main__":
    unittest.main()
