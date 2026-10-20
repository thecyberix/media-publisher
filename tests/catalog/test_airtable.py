from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from catalog_parser.airtable import (
    AirtableArchiveSource,
    AirtableClient,
    build_yt_description_comment,
    build_yt_title_comment,
    catalog_record_comments,
    catalog_record_to_airtable_fields,
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_NAME,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    collect_ingest_identity,
    load_existing_original_video_keys_for_ingest,
    load_existing_original_video_names_for_ingest,
    load_existing_titles_for_ingest,
    load_existing_video_folder_ids_for_ingest,
    normalize_original_video_name,
    normalize_title,
    normalize_title_variants,
    resolve_original_video_name,
)


class AirtableMappingTests(unittest.TestCase):
    def test_catalog_record_to_airtable_fields(self) -> None:
        fields = catalog_record_to_airtable_fields(
            {
                "ctLink": "https://example.com/video",
                "ctDuration": "120",
                "ctTitle": "Sample Title",
                "pkgLink": "https://drive.google.com/folder/1",
                "pkgBgSrtLk": "https://ea.smartcat.com/editor/1",
            }
        )
        self.assertEqual(fields["Original Video"], "https://example.com/video")
        self.assertEqual(fields["Duration"], 120)
        self.assertEqual(fields[FIELD_TITLE], "Sample Title")
        self.assertEqual(fields["Video Folder"], "https://drive.google.com/folder/1")
        self.assertEqual(fields["Translation resources"], "https://ea.smartcat.com/editor/1")
        self.assertEqual(fields["Type"], "Short")

    def test_catalog_record_maps_bg_title_and_description(self) -> None:
        from catalog_parser.airtable import (
            FIELD_VIDEO_CAPTION_TRANSLATED,
            FIELD_VIDEO_DESCRIPTION_TRANSLATED,
            FIELD_VIDEO_NAME_TRANSLATED,
        )

        fields = catalog_record_to_airtable_fields(
            {
                "ctTitle": "English Title",
                "ytTitle": "YT English Title",
                "ytDescription": "English description",
                "bgTitle": "Българско заглавие",
                "bgDescription": "Българско описание",
                "bgCaption": "БЪДЕТЕ\nРАДОСТНИ",
            }
        )
        self.assertEqual(fields[FIELD_VIDEO_NAME_TRANSLATED], "Българско заглавие")
        self.assertEqual(
            fields[FIELD_VIDEO_DESCRIPTION_TRANSLATED], "Българско описание"
        )
        self.assertEqual(fields[FIELD_VIDEO_CAPTION_TRANSLATED], "БЪДЕТЕ\nРАДОСТНИ")
        self.assertEqual(fields["Original Video Name"], "YT English Title")
        self.assertEqual(fields["Original Video Description"], "English description")

    def test_duration_to_type_boundaries(self) -> None:
        from catalog_parser.airtable import catalog_record_to_airtable_fields

        self.assertEqual(
            catalog_record_to_airtable_fields({"ctDuration": 90})["Type"], "Reel"
        )
        self.assertEqual(
            catalog_record_to_airtable_fields({"ctDuration": 91})["Type"], "Short"
        )
        self.assertEqual(
            catalog_record_to_airtable_fields({"ctDuration": 180})["Type"], "Short"
        )
        self.assertEqual(
            catalog_record_to_airtable_fields({"ctDuration": 181})["Type"], "Video"
        )

    def test_normalize_title_is_case_insensitive(self) -> None:
        self.assertEqual(normalize_title("  Hello World  "), "hello world")

    def test_normalize_title_variants_includes_sadhguru_stripped_form(self) -> None:
        self.assertEqual(
            normalize_title_variants("Sample Title | Sadhguru"),
            {"sample title | sadhguru", "sample title"},
        )

    def test_load_existing_titles_for_ingest_merges_archive_identity(self) -> None:
        from catalog_parser.workflow.archive_title_cache import ArchiveIngestIdentity

        client = AirtableClient("pat-test", "app-current", "Catalog")
        cache = type(
            "Cache",
            (),
            {
                "existing_title_keys": lambda self: {
                    "reel\tcurrent title",
                },
                "existing_video_folder_ids": lambda self: set(),
                "existing_original_video_names": lambda self: set(),
                "existing_original_video_keys": lambda self: set(),
                "records": [],
            },
        )()
        identity = ArchiveIngestIdentity(
            title_keys=frozenset({"reel\tarchived title"}),
            folder_ids=frozenset({"archive-folder"}),
            original_video_names=frozenset({"archive youtube name"}),
            original_video_keys=frozenset({"yt:archivedvid"}),
        )

        with patch(
            "catalog_parser.workflow.archive_title_cache.load_archive_identity",
            return_value=identity,
        ) as archive_mock:
            titles = load_existing_titles_for_ingest(
                client,
                table_cache=cache,
                archive_sources=[
                    AirtableArchiveSource(
                        base_id="app-archive",
                        table_name="Archive",
                        title_fields=("Original Video Name",),
                        identity_fields=("Original Video Name",),
                        title_from_original_video_name=True,
                    )
                ],
                project_root=Path("."),
            )

        self.assertIn("reel\tcurrent title", titles)
        self.assertIn("reel\tarchived title", titles)
        archive_mock.assert_called_once()

    def test_archive_identity_is_loaded_once_for_every_ingest_check(self) -> None:
        from catalog_parser.workflow.archive_title_cache import ArchiveIngestIdentity

        client = AirtableClient("pat-test", "app-current", "Catalog")
        cache = type(
            "Cache",
            (),
            {
                "existing_title_keys": lambda self: set(),
                "existing_video_folder_ids": lambda self: set(),
                "existing_original_video_names": lambda self: set(),
                "existing_original_video_keys": lambda self: set(),
                "records": [],
            },
        )()
        identity = ArchiveIngestIdentity(
            title_keys=frozenset({"reel\tarchived title"}),
            folder_ids=frozenset({"archive-folder"}),
            original_video_names=frozenset({"archive youtube name"}),
            original_video_keys=frozenset({"yt:archivedvid"}),
        )

        with patch(
            "catalog_parser.workflow.archive_title_cache.load_archive_identity",
            return_value=identity,
        ) as archive_mock:
            titles = load_existing_titles_for_ingest(
                client, table_cache=cache, project_root=Path(".")
            )
            folders = load_existing_video_folder_ids_for_ingest(
                client, table_cache=cache, project_root=Path(".")
            )
            names = load_existing_original_video_names_for_ingest(
                client, table_cache=cache, project_root=Path(".")
            )
            keys = load_existing_original_video_keys_for_ingest(
                client, table_cache=cache, project_root=Path(".")
            )

        archive_mock.assert_called_once()
        self.assertIn("reel\tarchived title", titles)
        self.assertEqual(folders, {"archive-folder"})
        self.assertEqual(names, {"archive youtube name"})
        self.assertEqual(keys, {"yt:archivedvid"})

    def test_load_existing_titles_uses_archive_file_cache_without_meta_api(self) -> None:
        from catalog_parser.workflow.archive_title_cache import (
            ArchiveIngestIdentity,
            archive_cache_path,
            write_archive_identity_cache,
        )
        from catalog_parser.workflow import archive_title_cache as cache_module

        cache_module._PROCESS_CACHE.clear()
        cache_module._UNVERIFIED_FILE_CACHE = None
        cache_module._UNVERIFIED_FILE_CACHE_PATH = None

        client = AirtableClient("pat-test", "app-current", "Catalog")
        cache = type(
            "Cache",
            (),
            {
                "existing_title_keys": lambda self: {"reel\tlive title"},
                "records": [],
            },
        )()

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            write_archive_identity_cache(
                archive_cache_path(root),
                sources=[
                    AirtableArchiveSource(
                        base_id="app-archive",
                        table_name="Archive",
                        title_fields=("Original Video Name",),
                        identity_fields=("Original Video Name",),
                        title_from_original_video_name=True,
                    )
                ],
                identity=ArchiveIngestIdentity(
                    title_keys=frozenset({"*\tarchived title"}),
                    folder_ids=frozenset(),
                    original_video_names=frozenset(),
                    original_video_keys=frozenset(),
                ),
            )
            with patch(
                "catalog_parser.workflow.archive_sources.resolve_archive_sources"
            ) as resolve_mock:
                titles = load_existing_titles_for_ingest(
                    client,
                    table_cache=cache,
                    project_root=root,
                )

        self.assertIn("reel\tlive title", titles)
        self.assertIn("*\tarchived title", titles)
        resolve_mock.assert_not_called()

    def test_list_base_tables_and_ensure_url_field_reuse_schema(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        tables = [
            {
                "id": "tbl1",
                "name": "Catalog",
                "fields": [{"name": "Translated subtitles", "type": "url"}],
            }
        ]
        with patch.object(client, "_request", return_value={"tables": tables}) as request_mock:
            self.assertFalse(client.ensure_url_field("Translated subtitles"))
            self.assertFalse(client.ensure_url_field("Translated subtitles"))
            self.assertEqual(client.list_base_tables("app-current"), tables)
        request_mock.assert_called_once()

    def test_normalize_original_video_name_strips_sadhguru_suffix(self) -> None:
        self.assertEqual(
            normalize_original_video_name("Temple vs Toilet: Which Is More Important? | Sadhguru"),
            "Temple vs Toilet: Which Is More Important?",
        )
        self.assertEqual(normalize_original_video_name("Plain Title"), "Plain Title")
        self.assertIsNone(normalize_original_video_name("   "))

    def test_resolve_original_video_name_prefers_yt_title(self) -> None:
        self.assertEqual(
            resolve_original_video_name(
                yt_title="YouTube Title | Sadhguru",
                title="Catalog Title",
            ),
            "YouTube Title",
        )

    def test_resolve_original_video_name_falls_back_to_title(self) -> None:
        self.assertEqual(
            resolve_original_video_name(yt_title=None, title="Catalog Title | Sadhguru"),
            "Catalog Title",
        )
        self.assertEqual(
            resolve_original_video_name(yt_title="", title="Catalog Title"),
            "Catalog Title",
        )

    def test_catalog_record_sets_original_video_name_without_suffix(self) -> None:
        fields = catalog_record_to_airtable_fields(
            {
                "ctTitle": "Catalog Title",
                "ytTitle": "YouTube Title | Sadhguru",
                "ctDuration": "120",
            }
        )
        self.assertEqual(fields[FIELD_TITLE], "Catalog Title")
        self.assertEqual(fields["Original Video Name"], "YouTube Title")

    def test_catalog_record_sets_original_video_description(self) -> None:
        fields = catalog_record_to_airtable_fields(
            {
                "ctTitle": "Catalog Title",
                "ctDuration": "120",
                "ytDescription": "Original YouTube description",
            }
        )
        self.assertEqual(fields["Original Video Description"], "Original YouTube description")

    def test_catalog_record_sets_original_video_thumbnail(self) -> None:
        fields = catalog_record_to_airtable_fields(
            {
                "ctTitle": "Catalog Title",
                "ctDuration": "120",
                "ytThumbnail": [{"url": "https://cdn.example/thumb.jpg"}],
            }
        )
        self.assertEqual(
            fields["Original Video Thumbnail"],
            [{"url": "https://cdn.example/thumb.jpg"}],
        )

    def test_build_yt_title_comment(self) -> None:
        self.assertIsNone(build_yt_title_comment(None))
        self.assertIsNone(build_yt_title_comment(""))
        self.assertIsNone(build_yt_title_comment("   "))
        self.assertEqual(
            build_yt_title_comment("My YouTube Title"),
            "Заглавие:\nMy YouTube Title",
        )

    def test_build_yt_description_comment(self) -> None:
        self.assertEqual(
            build_yt_description_comment(
                {"ctDuration": 200, "ytDescription": "A long-form description."}
            ),
            "Описание:\nA long-form description.",
        )
        self.assertEqual(
            build_yt_description_comment(
                {"ctDuration": 120, "ytDescription": "A short-form description."}
            ),
            "Описание:\nA short-form description.",
        )
        self.assertEqual(
            build_yt_description_comment(
                {"ctDuration": 60, "ytDescription": "A reel description."}
            ),
            "Описание:\nA reel description.",
        )
        self.assertIsNone(build_yt_description_comment({"ctDuration": 200}))
        self.assertIsNone(
            build_yt_description_comment({"ctDuration": 200, "ytDescription": ""})
        )

    def test_catalog_record_comments_order(self) -> None:
        comments = catalog_record_comments(
            {
                "ctDuration": 200,
                "ytTitle": "YT Title",
                "ytDescription": "YT Description",
            }
        )
        self.assertEqual(
            comments,
            [
                "Заглавие:\nYT Title",
                "Описание:\nYT Description",
            ],
        )

    def test_catalog_record_comments_falls_back_to_ct_title(self) -> None:
        comments = catalog_record_comments(
            {
                "ctTitle": "Original Catalog Title",
                "ytTitle": None,
            }
        )
        self.assertEqual(comments, ["Заглавие:\nOriginal Catalog Title"])


class AirtableIdentityListTests(unittest.TestCase):
    def test_duplicate_checks_share_one_table_list(self) -> None:
        from catalog_parser.airtable import (
            FIELD_ORIGINAL_VIDEO,
            FIELD_ORIGINAL_VIDEO_NAME,
            FIELD_TYPE,
            FIELD_VIDEO_FOLDER,
        )

        client = AirtableClient("pat-test", "app-identity", "Catalog")
        client._clear_ingest_identity_cache()
        page = {
            "records": [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Hello",
                        FIELD_TYPE: "Reel",
                        FIELD_VIDEO_FOLDER: "folder123",
                        FIELD_ORIGINAL_VIDEO_NAME: "Hello | Sadhguru",
                        FIELD_ORIGINAL_VIDEO: "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    },
                }
            ]
        }
        with patch.object(client, "_request", return_value=page) as request_mock:
            titles = client.list_existing_titles()
            folders = client.list_existing_video_folder_ids()
            names = client.list_existing_original_video_names()
            keys = client.list_existing_original_video_keys()
            other = AirtableClient("pat-test", "app-identity", "Catalog")
            with patch.object(other, "_request") as second_mock:
                self.assertEqual(other.list_existing_titles(), titles)

        self.assertEqual(request_mock.call_count, 1)
        self.assertEqual(second_mock.call_count, 0)
        self.assertIn("reel\thello", titles)
        self.assertEqual(folders, {"folder123"})
        self.assertIn("hello", names)
        self.assertTrue(any(key.startswith("yt:") for key in keys))
        query = request_mock.call_args.kwargs["query"]
        self.assertEqual(query["pageSize"], "100")
        self.assertIn(FIELD_TITLE, query["fields[]"])
        self.assertIn(FIELD_VIDEO_FOLDER, query["fields[]"])
        client._clear_ingest_identity_cache()

    def test_missing_archive_values_are_omitted_per_row(self) -> None:
        titled, folders, names, videos = collect_ingest_identity(
            {
                FIELD_TITLE: "Has Title",
                FIELD_TYPE: "Video",
                FIELD_ORIGINAL_VIDEO_NAME: "YouTube Name | Sadhguru",
                FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/folder1",
                FIELD_ORIGINAL_VIDEO: "https://youtu.be/dQw4w9WgXcQ",
            }
        )
        self.assertIn("video\thas title", titled)
        self.assertEqual(folders, {"folder1"})
        self.assertEqual(names, {"youtube name"})
        self.assertTrue(any(key.startswith("yt:") for key in videos))

        sparse_titles, sparse_folders, sparse_names, sparse_videos = collect_ingest_identity(
            {FIELD_TITLE: "Only Title", FIELD_TYPE: "Reel"}
        )
        self.assertEqual(sparse_titles, {"reel\tonly title"})
        self.assertEqual(sparse_folders, set())
        self.assertEqual(sparse_names, set())
        self.assertEqual(sparse_videos, set())

        old_titles, _old_folders, old_names, _old_videos = collect_ingest_identity(
            {FIELD_ORIGINAL_VIDEO_NAME: "Legacy Title | Sadhguru"},
            title_from_original_video_name=True,
        )
        self.assertIn("*\tlegacy title", old_titles)
        self.assertIn("*\tlegacy title | sadhguru", old_titles)
        self.assertEqual(old_names, set())


class AirtableSyncTests(unittest.TestCase):
    def test_sync_skips_existing_titles_and_creates_new_rows(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        records = [
            {"ctTitle": "Already There", "ctLink": "https://a"},
            {"ctTitle": "New Entry", "ctLink": "https://b", "ctDuration": 40},
        ]

        with patch(
            "catalog_parser.airtable.load_existing_titles_for_ingest",
            return_value={"*\talready there"},
        ), patch(
            "catalog_parser.airtable.load_existing_video_folder_ids_for_ingest",
            return_value=set(),
        ), patch(
            "catalog_parser.airtable.load_existing_original_video_names_for_ingest",
            return_value=set(),
        ), patch(
            "catalog_parser.airtable.load_existing_original_video_keys_for_ingest",
            return_value=set(),
        ):
            with patch.object(client, "create_records", return_value=["recNEW"]) as create_mock:
                created, skipped = client.sync_catalog_records(records)

        self.assertEqual(created, 1)
        self.assertEqual(skipped, 1)
        create_mock.assert_called_once_with([records[1]])

    def test_create_records_skips_comments_by_default(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        records = [
            {
                "ctTitle": "New Entry",
                "ctLink": "https://b",
                "ytTitle": "YouTube Title Here",
            },
        ]

        with patch.object(client, "_request") as request_mock:
            request_mock.return_value = {
                "records": [{"id": "recAAA", "fields": {}}],
            }
            created = client.create_records(records)

        self.assertEqual(created, ["recAAA"])
        self.assertEqual(request_mock.call_count, 1)

    def test_create_records_can_write_comments_when_requested(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        records = [
            {
                "ctTitle": "New Entry",
                "ctLink": "https://b",
                "ytTitle": "YouTube Title Here",
            },
        ]

        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {"records": [{"id": "recAAA", "fields": {}}]},
                {},
            ]
            created = client.create_records(records, write_comments=True)

        self.assertEqual(created, ["recAAA"])
        self.assertEqual(request_mock.call_count, 2)
        comment_call = request_mock.call_args_list[1]
        self.assertEqual(comment_call.args[0], "POST")
        self.assertIn("/recAAA/comments", comment_call.args[1])


if __name__ == "__main__":
    unittest.main()
