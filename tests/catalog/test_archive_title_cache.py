from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from catalog_parser.airtable import AirtableArchiveSource, AirtableClient, FIELD_ORIGINAL_VIDEO_NAME, FIELD_TITLE
from catalog_parser.workflow import archive_title_cache as cache_module
from catalog_parser.workflow.archive_title_cache import (
    archive_cache_path,
    fetch_archive_titles,
    load_archive_titles,
    read_archive_title_cache,
    write_archive_title_cache,
)


class ArchiveTitleCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_module._PROCESS_CACHE.clear()

    def test_write_and_read_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sources = [
                AirtableArchiveSource(
                    base_id="app-archive",
                    table_name="Archive",
                    title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
                )
            ]
            path = write_archive_title_cache(
                archive_cache_path(root),
                sources=sources,
                titles={"archived title", "another title"},
                fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )

            loaded = read_archive_title_cache(path, sources=sources)

        self.assertEqual(loaded, {"archived title", "another title"})

    def test_read_cache_stays_valid_without_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sources = [
                AirtableArchiveSource(
                    base_id="app-archive",
                    table_name="Archive",
                    title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
                )
            ]
            path = write_archive_title_cache(
                archive_cache_path(root),
                sources=sources,
                titles={"archived title"},
                fetched_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )

            loaded = read_archive_title_cache(path, sources=sources)

        self.assertEqual(loaded, {"archived title"})

    def test_read_cache_returns_none_when_source_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = write_archive_title_cache(
                archive_cache_path(root),
                sources=[
                    AirtableArchiveSource(
                        base_id="app-archive",
                        table_name="Archive",
                        title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
                    )
                ],
                titles={"archived title"},
            )

            loaded = read_archive_title_cache(
                path,
                sources=[
                    AirtableArchiveSource(
                        base_id="app-other",
                        table_name="Archive",
                        title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
                    )
                ],
            )

        self.assertIsNone(loaded)

    def test_load_archive_titles_uses_file_cache_without_api(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [
            AirtableArchiveSource(
                base_id="app-archive",
                table_name="Archive",
                title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
            )
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_title_cache(
                archive_cache_path(root),
                sources=sources,
                titles={"cached archive title"},
            )

            with patch.object(client, "list_title_variants") as variants_mock:
                titles = load_archive_titles(client, sources, project_root=root)

        self.assertEqual(titles, {"cached archive title"})
        variants_mock.assert_not_called()

    def test_load_archive_titles_reuses_process_cache(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [
            AirtableArchiveSource(
                base_id="app-archive",
                table_name="Archive",
                title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
            )
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch(
                "catalog_parser.workflow.archive_title_cache.fetch_archive_titles",
                return_value={"live archive title"},
            ) as fetch_mock:
                first = load_archive_titles(client, sources, project_root=root)
                second = load_archive_titles(client, sources, project_root=root)

        self.assertEqual(first, {"live archive title"})
        self.assertEqual(second, {"live archive title"})
        fetch_mock.assert_called_once()

    def test_load_archive_titles_force_refresh_bypasses_file_cache(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [
            AirtableArchiveSource(
                base_id="app-archive",
                table_name="Archive",
                title_fields=(FIELD_ORIGINAL_VIDEO_NAME,),
            )
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_title_cache(
                archive_cache_path(root),
                sources=sources,
                titles={"stale archive title"},
            )

            with patch(
                "catalog_parser.workflow.archive_title_cache.fetch_archive_titles",
                return_value={"fresh archive title"},
            ) as fetch_mock:
                titles = load_archive_titles(
                    client,
                    sources,
                    project_root=root,
                    force_refresh=True,
                )

        self.assertEqual(titles, {"fresh archive title"})
        fetch_mock.assert_called_once()

    def test_fetch_archive_titles_merges_all_sources(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [
            AirtableArchiveSource("app-a", "Table A", (FIELD_TITLE,)),
            AirtableArchiveSource("app-b", "Table B", (FIELD_ORIGINAL_VIDEO_NAME,)),
        ]

        with patch.object(
            client,
            "list_title_variants",
            side_effect=[{"title a"}, {"title b"}],
        ) as variants_mock:
            titles = fetch_archive_titles(client, sources)

        self.assertEqual(titles, {"title a", "title b"})
        self.assertEqual(variants_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
