from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from catalog_parser.airtable import (
    AirtableArchiveSource,
    AirtableClient,
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.workflow import archive_title_cache as cache_module
from catalog_parser.workflow.archive_title_cache import (
    ArchiveIngestIdentity,
    archive_cache_path,
    fetch_archive_identity,
    load_archive_identity,
    peek_archive_identity_cache,
    read_archive_identity_cache,
    write_archive_identity_cache,
)


def _source(**overrides: object) -> AirtableArchiveSource:
    values: dict[str, object] = {
        "base_id": "app-archive",
        "table_name": "Archive",
        "title_fields": (FIELD_ORIGINAL_VIDEO_NAME,),
        "identity_fields": (FIELD_ORIGINAL_VIDEO_NAME,),
        "title_from_original_video_name": True,
    }
    values.update(overrides)
    return AirtableArchiveSource(**values)  # type: ignore[arg-type]


def _identity(**overrides: object) -> ArchiveIngestIdentity:
    values: dict[str, frozenset[str]] = {
        "title_keys": frozenset({"*\tarchived title"}),
        "folder_ids": frozenset(),
        "original_video_names": frozenset(),
        "original_video_keys": frozenset(),
    }
    values.update(overrides)  # type: ignore[arg-type]
    return ArchiveIngestIdentity(**values)  # type: ignore[arg-type]


class ArchiveIdentityCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        cache_module._PROCESS_CACHE.clear()
        cache_module._UNVERIFIED_FILE_CACHE = None
        cache_module._UNVERIFIED_FILE_CACHE_PATH = None

    def test_write_and_read_cache_round_trip(self) -> None:
        identity = _identity(
            title_keys=frozenset({"reel\thello", "*\tother"}),
            folder_ids=frozenset({"folder-1"}),
            original_video_names=frozenset({"youtube name"}),
            original_video_keys=frozenset({"yt:abcdefghijk"}),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sources = [_source()]
            path = write_archive_identity_cache(
                archive_cache_path(root),
                sources=sources,
                identity=identity,
                fetched_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            )

            loaded = read_archive_identity_cache(path, sources=sources)

        self.assertEqual(loaded, identity)

    def test_read_cache_stays_valid_without_expiry(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            sources = [_source()]
            path = write_archive_identity_cache(
                archive_cache_path(root),
                sources=sources,
                identity=identity,
                fetched_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )

            loaded = read_archive_identity_cache(path, sources=sources)

        self.assertEqual(loaded, identity)

    def test_read_cache_returns_none_when_source_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = write_archive_identity_cache(
                archive_cache_path(root),
                sources=[_source()],
                identity=_identity(),
            )

            loaded = read_archive_identity_cache(
                path,
                sources=[_source(base_id="app-other")],
            )

        self.assertIsNone(loaded)

    def test_title_only_cache_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            path = archive_cache_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-09-04T00:03:49+00:00",
                        "title_count": 1,
                        "sources": [
                            {
                                "base_id": "app-archive",
                                "table_name": "Archive",
                                "title_fields": ["Original Video Name"],
                            }
                        ],
                        "titles": ["archived title"],
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(peek_archive_identity_cache(path))
            self.assertIsNone(read_archive_identity_cache(path, sources=[_source()]))

    def test_load_archive_identity_uses_file_cache_without_api(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [_source()]
        identity = _identity()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_identity_cache(
                archive_cache_path(root),
                sources=sources,
                identity=identity,
            )

            with patch.object(client, "list_records") as records_mock:
                loaded = load_archive_identity(client, sources, project_root=root)

        self.assertEqual(loaded, identity)
        records_mock.assert_not_called()

    def test_load_archive_identity_reuses_process_cache(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [_source()]
        identity = _identity(title_keys=frozenset({"*\tlive archive title"}))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch(
                "catalog_parser.workflow.archive_title_cache.fetch_archive_identity",
                return_value=identity,
            ) as fetch_mock:
                first = load_archive_identity(client, sources, project_root=root)
                second = load_archive_identity(client, sources, project_root=root)

        self.assertEqual(first, identity)
        self.assertEqual(second, identity)
        fetch_mock.assert_called_once()

    def test_load_archive_identity_force_refresh_bypasses_file_cache(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [_source()]
        fresh = _identity(title_keys=frozenset({"*\tfresh archive title"}))

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_identity_cache(
                archive_cache_path(root),
                sources=sources,
                identity=_identity(title_keys=frozenset({"*\tstale archive title"})),
            )

            with patch(
                "catalog_parser.workflow.archive_title_cache.fetch_archive_identity",
                return_value=fresh,
            ) as fetch_mock:
                loaded = load_archive_identity(
                    client,
                    sources,
                    project_root=root,
                    force_refresh=True,
                )

        self.assertEqual(loaded, fresh)
        fetch_mock.assert_called_once()

    def test_fetch_lists_each_source_once(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        sources = [
            AirtableArchiveSource(
                "app-a",
                "Table A",
                (FIELD_TITLE,),
                identity_fields=(FIELD_TITLE, FIELD_TYPE, FIELD_VIDEO_FOLDER),
            ),
            AirtableArchiveSource(
                "app-b",
                "Table B",
                (FIELD_ORIGINAL_VIDEO_NAME,),
                identity_fields=(FIELD_ORIGINAL_VIDEO_NAME, FIELD_ORIGINAL_VIDEO),
                title_from_original_video_name=True,
            ),
        ]

        def _page(records: list[dict[str, object]]) -> dict[str, object]:
            return {"records": records}

        with patch.object(
            client,
            "_request",
            side_effect=[
                _page(
                    [
                        {
                            "id": "recA",
                            "fields": {
                                FIELD_TITLE: "Title A",
                                FIELD_TYPE: "Reel",
                                FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/folderA",
                            },
                        }
                    ]
                ),
                _page(
                    [
                        {
                            "id": "recB",
                            "fields": {
                                FIELD_ORIGINAL_VIDEO_NAME: "Old Title",
                                FIELD_ORIGINAL_VIDEO: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                            },
                        }
                    ]
                ),
            ],
        ) as request_mock:
            identity = fetch_archive_identity(client, sources)

        self.assertEqual(request_mock.call_count, 2)
        self.assertIn("reel\ttitle a", identity.title_keys)
        self.assertIn("*\told title", identity.title_keys)
        self.assertIn("folderA", identity.folder_ids)
        self.assertEqual(identity.original_video_names, frozenset())
        self.assertTrue(any(key.startswith("yt:") for key in identity.original_video_keys))
        first_query = request_mock.call_args_list[0].kwargs["query"]
        self.assertEqual(
            first_query["fields[]"],
            [FIELD_TITLE, FIELD_TYPE, FIELD_VIDEO_FOLDER],
        )

    def test_peek_does_not_require_matching_sources(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_identity_cache(
                archive_cache_path(root),
                sources=[_source()],
                identity=identity,
            )
            loaded = peek_archive_identity_cache(archive_cache_path(root))

        self.assertEqual(loaded, identity)

    def test_warm_cache_peek_skips_source_resolution(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        identity = _identity(
            folder_ids=frozenset({"folder-9"}),
            original_video_keys=frozenset({"ig:abc"}),
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_identity_cache(
                archive_cache_path(root),
                sources=[_source()],
                identity=identity,
            )
            with patch(
                "catalog_parser.workflow.archive_sources.resolve_archive_sources"
            ) as resolve_mock:
                loaded = load_archive_identity(client, None, project_root=root)

        self.assertEqual(loaded, identity)
        resolve_mock.assert_not_called()
        self.assertEqual(client.request_count, 0)


if __name__ == "__main__":
    unittest.main()
