from __future__ import annotations

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
    FIELD_TITLE,
    load_existing_titles_for_ingest,
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

    def test_load_existing_titles_for_ingest_merges_archive_titles(self) -> None:
        client = AirtableClient("pat-test", "app-current", "Catalog")
        cache = type("Cache", (), {"existing_titles": lambda self: {"current title"}})()

        with patch(
            "catalog_parser.workflow.archive_title_cache.load_archive_titles",
            return_value={"archived title"},
        ) as archive_mock:
            titles = load_existing_titles_for_ingest(
                client,
                table_cache=cache,
                archive_sources=[
                    AirtableArchiveSource(
                        base_id="app-archive",
                        table_name="Archive",
                        title_fields=("Original Video Name",),
                    )
                ],
                project_root=Path("."),
            )

        self.assertEqual(titles, {"current title", "archived title"})
        archive_mock.assert_called_once()

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


class AirtableSyncTests(unittest.TestCase):
    def test_sync_skips_existing_titles_and_creates_new_rows(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        records = [
            {"ctTitle": "Already There", "ctLink": "https://a"},
            {"ctTitle": "New Entry", "ctLink": "https://b", "ctDuration": 40},
        ]

        with patch(
            "catalog_parser.airtable.load_existing_titles_for_ingest",
            return_value={"already there"},
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
