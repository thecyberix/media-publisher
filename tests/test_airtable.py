from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from media_publisher.models import PlatformScheduleTask
from media_publisher.sources.airtable import (
    AirtableClient,
    AirtableRecord,
    DEFAULT_PUBLISH_HOUR,
    DEFAULT_PUBLISH_TIMEZONE,
    FIELD_SG_FB_DATE,
    FIELD_SG_FB_PUBLISHED,
    FIELD_SG_IG_DATE,
    FIELD_SG_IG_PUBLISHED,
    FIELD_SG_YT_DATE,
    FIELD_SG_YT_PUBLISHED,
    FIELD_SMEDIA_UPLOADED,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VIDEO_NAME_TRANSLATED,
    SMEDIA_OPTION_FACEBOOK,
    SMEDIA_OPTION_YOUTUBE,
    STATUS_SYNC_DONE,
    TYPE_REEL,
    TYPE_QUOTE,
    TYPE_VIDEO,
    auto_schedule_tomorrow_catalog_item,
    build_platform_published_update,
    catalog_instagram_schedule_excluded,
    fetch_missing_translation_reports,
    fetch_pending_schedule_tasks,
    mark_record_done_and_published_if_complete,
    missing_translation_report,
    pending_schedule_filter_formula,
    record_publish_platforms_complete,
    record_schedule_tasks,
    record_to_publish_job,
    video_format_from_type,
    _parse_publish_at,
)


class AirtableMappingTests(unittest.TestCase):
    def test_parse_publish_at_uses_six_pm_local(self) -> None:
        publish_at = _parse_publish_at("2026-07-05")
        self.assertIsNotNone(publish_at)
        assert publish_at is not None
        local = publish_at.astimezone(ZoneInfo(DEFAULT_PUBLISH_TIMEZONE))
        self.assertEqual(local.hour, DEFAULT_PUBLISH_HOUR)
        self.assertEqual(local.date().isoformat(), "2026-07-05")

    def test_video_format_from_type(self) -> None:
        self.assertEqual(video_format_from_type(TYPE_VIDEO), "post")
        self.assertEqual(video_format_from_type(TYPE_REEL), "short_form")
        self.assertEqual(video_format_from_type("Short"), "short_form")

    def test_record_to_publish_job(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    FIELD_TITLE: "Sample Title",
                    "Video name translated": "Преведено заглавие",
                    "Video description translated": "Преведено описание",
                    "Original Video": "https://example.com/video",
                    "Duration": 120,
                    "Type": "Short",
                    "Video Folder": "https://drive.google.com/folder/1",
                    "Translation resources": "https://ea.smartcat.com/editor/1",
                },
            )
        )
        self.assertEqual(job.title, "Преведено заглавие")
        self.assertEqual(job.description, "Преведено описание")
        self.assertEqual(job.metadata[FIELD_TITLE], "Sample Title")
        self.assertEqual(job.video_url, "https://example.com/video")
        self.assertEqual(job.airtable_record_id, "recABC")
        self.assertEqual(job.tags, [])
        self.assertEqual(job.video_format, "short_form")
        self.assertEqual(job.metadata["Duration"], "120")
        self.assertEqual(
            job.metadata["Video Folder"],
            "https://drive.google.com/folder/1",
        )

    def test_record_to_publish_job_leaves_title_empty_without_translation(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    FIELD_TITLE: "Sample Title",
                },
            )
        )
        self.assertEqual(job.title, "")
        self.assertEqual(job.metadata[FIELD_TITLE], "Sample Title")

    def test_record_to_publish_job_maps_canva_design(self) -> None:
        job = record_to_publish_job(
            AirtableRecord(
                id="recABC",
                fields={
                    FIELD_TITLE: "Sample Title",
                    "Canva Design": "https://www.canva.com/design/DAGabc123/view",
                },
            )
        )
        self.assertEqual(
            job.metadata["canva_design_id"],
            "https://www.canva.com/design/DAGabc123/view",
        )
        self.assertEqual(
            job.metadata["Canva Design"],
            "https://www.canva.com/design/DAGabc123/view",
        )


class CatalogScheduleTests(unittest.TestCase):
    def test_auto_schedule_tomorrow_prefers_thumbnail_and_sets_dates(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        # Two unscheduled reel records; prefer the one with "Original Video Thumbnail".
        records = [
            AirtableRecord(
                id="rec-no-thumb",
                created_time="2026-07-01T10:00:00.000Z",
                fields={
                    FIELD_STATUS: "5. Synchronization done",
                    FIELD_TITLE: "A",
                    FIELD_VIDEO_NAME_TRANSLATED: "A tr",
                    "Type": TYPE_REEL,
                },
            ),
            AirtableRecord(
                id="rec-thumb",
                created_time="2026-07-02T10:00:00.000Z",
                fields={
                    FIELD_STATUS: "5. Synchronization done",
                    FIELD_TITLE: "B",
                    FIELD_VIDEO_NAME_TRANSLATED: "B tr",
                    "Type": TYPE_REEL,
                    "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
                },
            ),
        ]
        with (
            patch.object(client, "list_records", return_value=records),
            patch.object(client, "update_record") as update_mock,
        ):
            update_mock.return_value = AirtableRecord(id="rec-thumb", fields={})
            scheduled = auto_schedule_tomorrow_catalog_item(
                client,
                target_date=date(2026, 7, 6),  # Monday => Reel
                publish_timezone="UTC",
                publish_hour=18,
            )
        self.assertIsNotNone(scheduled)
        update_mock.assert_called_once()
        args = update_mock.call_args.args
        kwargs = update_mock.call_args.kwargs
        self.assertEqual(args[0], "rec-thumb")
        fields = args[1] if len(args) > 1 else kwargs["fields"]
        self.assertEqual(fields[FIELD_SG_YT_DATE], "2026-07-06")
        self.assertEqual(fields[FIELD_SG_FB_DATE], "2026-07-06")
        self.assertEqual(fields[FIELD_SG_IG_DATE], "2026-07-06")

    def test_auto_schedule_tomorrow_omits_instagram_for_long_videos(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        records = [
            AirtableRecord(
                id="rec-long",
                created_time="2026-07-01T10:00:00.000Z",
                fields={
                    FIELD_STATUS: "5. Synchronization done",
                    FIELD_TITLE: "Long talk",
                    FIELD_VIDEO_NAME_TRANSLATED: "Long tr",
                    "Type": TYPE_VIDEO,
                    "Duration": 1487,
                },
            ),
        ]
        with (
            patch.object(client, "list_records", return_value=records),
            patch.object(client, "update_record") as update_mock,
        ):
            update_mock.return_value = AirtableRecord(id="rec-long", fields={})
            scheduled = auto_schedule_tomorrow_catalog_item(
                client,
                target_date=date(2026, 7, 11),  # Saturday => Video
                publish_timezone="UTC",
                publish_hour=18,
            )
        self.assertIsNotNone(scheduled)
        fields = update_mock.call_args.args[1]
        self.assertEqual(fields[FIELD_SG_YT_DATE], "2026-07-11")
        self.assertEqual(fields[FIELD_SG_FB_DATE], "2026-07-11")
        self.assertNotIn(FIELD_SG_IG_DATE, fields)

    def test_catalog_instagram_schedule_excluded(self) -> None:
        self.assertTrue(
            catalog_instagram_schedule_excluded({"Duration": 16 * 60})
        )
        self.assertFalse(
            catalog_instagram_schedule_excluded({"Duration": 10 * 60})
        )

    def test_auto_schedule_tomorrow_noop_when_pending_already_exists(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        # Pending short-form task already scheduled for target date.
        pending_record = AirtableRecord(
            id="rec-pending",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Already scheduled",
                FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                FIELD_SG_YT_DATE: "2026-07-06",
                "Type": TYPE_REEL,
            },
        )
        with (
            patch.object(client, "list_records", return_value=[pending_record]),
            patch.object(client, "update_record") as update_mock,
        ):
            scheduled = auto_schedule_tomorrow_catalog_item(
                client,
                target_date=date(2026, 7, 6),
                publish_timezone="UTC",
                publish_hour=18,
            )
        self.assertIsNone(scheduled)
        update_mock.assert_not_called()

    def test_record_schedule_tasks_for_sync_done_row(self) -> None:
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_VIDEO_NAME_TRANSLATED: "Видео за стартиране",
                FIELD_SG_YT_DATE: "2026-07-05",
                FIELD_SG_FB_DATE: "2026-07-06",
            },
        )
        tasks = record_schedule_tasks(record)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].platform, "youtube")
        self.assertEqual(tasks[0].publish_at.astimezone(ZoneInfo(DEFAULT_PUBLISH_TIMEZONE)).hour, 18)
        self.assertEqual(tasks[1].platform, "facebook")
        self.assertEqual(tasks[1].job.title, "Видео за стартиране")

    def test_record_schedule_tasks_skips_missing_translation(self) -> None:
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_SG_YT_DATE: "2026-07-05",
            },
        )
        self.assertEqual(record_schedule_tasks(record), [])

    def test_missing_translation_report(self) -> None:
        report = missing_translation_report(
            AirtableRecord(
                id="recABC",
                fields={
                    FIELD_STATUS: "5. Synchronization done",
                    FIELD_TITLE: "Launch video",
                    FIELD_SG_YT_DATE: "2026-07-05",
                    FIELD_SG_FB_DATE: "2026-07-06",
                },
            )
        )
        self.assertIsNotNone(report)
        assert report is not None
        self.assertEqual(report.record_id, "recABC")
        self.assertEqual(report.original_title, "Launch video")
        self.assertEqual(report.platforms, ("youtube", "facebook"))

    def test_fetch_missing_translation_reports(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_SG_IG_DATE: "2026-07-07",
            },
        )
        with patch.object(client, "list_records", return_value=[record]):
            reports = fetch_missing_translation_reports(client)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].platforms, ("instagram",))

    def test_record_schedule_tasks_skips_published_platform(self) -> None:
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_VIDEO_NAME_TRANSLATED: "Видео за стартиране",
                FIELD_SG_YT_DATE: "2026-07-05",
                FIELD_SG_YT_PUBLISHED: "https://www.youtube.com/watch?v=abc123",
            },
        )
        tasks = record_schedule_tasks(record)
        self.assertEqual(tasks, [])

    def test_record_schedule_tasks_ignores_other_statuses(self) -> None:
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "In progress",
                FIELD_SG_YT_DATE: "2026-07-05",
            },
        )
        self.assertEqual(record_schedule_tasks(record), [])

    def test_record_schedule_tasks_accepts_numbered_sync_done_status(self) -> None:
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_VIDEO_NAME_TRANSLATED: "Видео за стартиране",
                FIELD_SG_YT_DATE: "2026-07-05",
            },
        )
        tasks = record_schedule_tasks(record)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].platform, "youtube")

    def test_record_publish_platforms_complete(self) -> None:
        complete_fields = {
            FIELD_SG_YT_DATE: "2026-07-05",
            FIELD_SG_FB_DATE: "2026-07-05",
            FIELD_SG_IG_DATE: "2026-07-05",
            FIELD_SG_YT_PUBLISHED: "https://youtube.com/watch?v=1",
            FIELD_SG_FB_PUBLISHED: "https://facebook.com/watch/?v=1",
            FIELD_SG_IG_PUBLISHED: "https://instagram.com/p/1",
        }
        self.assertTrue(record_publish_platforms_complete(complete_fields))

        partial_fields = dict(complete_fields)
        partial_fields.pop(FIELD_SG_FB_PUBLISHED)
        self.assertFalse(record_publish_platforms_complete(partial_fields))

        private_fields = dict(complete_fields)
        private_fields.pop(FIELD_SG_IG_PUBLISHED)
        self.assertTrue(
            record_publish_platforms_complete(
                private_fields,
                excluded_platforms=frozenset({"instagram"}),
            )
        )

        yt_fb_only_fields = {
            FIELD_SG_YT_DATE: "2026-07-05",
            FIELD_SG_FB_DATE: "2026-07-05",
            FIELD_SG_YT_PUBLISHED: "https://youtube.com/watch?v=1",
            FIELD_SG_FB_PUBLISHED: "https://facebook.com/watch/?v=1",
        }
        self.assertFalse(record_publish_platforms_complete(yt_fb_only_fields))

        long_form_fields = {
            **yt_fb_only_fields,
            "Duration": 16 * 60,
        }
        self.assertTrue(record_publish_platforms_complete(long_form_fields))

    def test_mark_record_done_and_published_if_complete(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        record_fields = {
            FIELD_STATUS: "5. Synchronization done",
            FIELD_SG_YT_DATE: "2026-07-05",
            FIELD_SG_FB_DATE: "2026-07-05",
            FIELD_SG_IG_DATE: "2026-07-05",
            FIELD_SG_YT_PUBLISHED: "https://youtube.com/watch?v=1",
            FIELD_SG_FB_PUBLISHED: "https://facebook.com/watch/?v=1",
            FIELD_SG_IG_PUBLISHED: "https://instagram.com/p/1",
        }
        with patch.object(
            client,
            "update_record",
            return_value=AirtableRecord(
                id="recABC",
                fields={FIELD_STATUS: "6. Done & Published"},
            ),
        ) as update_mock:
            updated = mark_record_done_and_published_if_complete(
                client,
                record_id="recABC",
                record_fields=record_fields,
            )
        self.assertIsNotNone(updated)
        update_mock.assert_called_once_with(
            "recABC",
            {FIELD_STATUS: "6. Done & Published"},
        )

        update_mock.reset_mock()
        not_updated = mark_record_done_and_published_if_complete(
            client,
            record_id="recABC",
            record_fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_SG_YT_DATE: "2026-07-05",
                FIELD_SG_FB_DATE: "2026-07-06",
                FIELD_SG_YT_PUBLISHED: "https://youtube.com/watch?v=1",
            },
        )
        self.assertIsNone(not_updated)
        update_mock.assert_not_called()

    def test_build_platform_published_update_merges_smedia_uploaded(self) -> None:
        update = build_platform_published_update(
            {
                FIELD_SMEDIA_UPLOADED: [SMEDIA_OPTION_YOUTUBE],
            },
            "facebook",
            "https://www.facebook.com/watch/?v=123",
        )
        self.assertEqual(
            update[FIELD_SG_FB_PUBLISHED],
            "https://www.facebook.com/watch/?v=123",
        )
        self.assertEqual(
            update[FIELD_SMEDIA_UPLOADED],
            [SMEDIA_OPTION_YOUTUBE, SMEDIA_OPTION_FACEBOOK],
        )

    def test_pending_schedule_filter_formula(self) -> None:
        formula = pending_schedule_filter_formula()
        self.assertIn(STATUS_SYNC_DONE, formula)
        self.assertIn(FIELD_SG_YT_DATE, formula)
        self.assertIn(FIELD_SG_FB_PUBLISHED, formula)

    def test_pending_schedule_filter_formula_videos_only(self) -> None:
        formula = pending_schedule_filter_formula(content_type="video")
        self.assertIn(f'{{Type}} != "{TYPE_QUOTE}"', formula)

    def test_fetch_pending_schedule_tasks_limits_platform(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_VIDEO_NAME_TRANSLATED: "Видео за стартиране",
                FIELD_SG_YT_DATE: "2026-07-07",
                FIELD_SG_FB_DATE: "2026-07-07",
            },
        )
        with patch.object(client, "list_records", return_value=[record]):
            tasks = fetch_pending_schedule_tasks(client, platforms=("youtube",))

        self.assertEqual([task.platform for task in tasks], ["youtube"])

    def test_fetch_pending_schedule_tasks(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        record = AirtableRecord(
            id="recABC",
            fields={
                FIELD_STATUS: "5. Synchronization done",
                FIELD_TITLE: "Launch video",
                FIELD_VIDEO_NAME_TRANSLATED: "Видео за стартиране",
                FIELD_SG_IG_DATE: "2026-07-07",
            },
        )
        with patch.object(client, "list_records", return_value=[record]) as list_mock:
            tasks = fetch_pending_schedule_tasks(client)

        self.assertEqual(len(tasks), 1)
        self.assertIsInstance(tasks[0], PlatformScheduleTask)
        self.assertEqual(tasks[0].platform, "instagram")
        self.assertEqual(
            tasks[0].publish_at,
            datetime(2026, 7, 7, 15, 0, tzinfo=timezone.utc),
        )
        list_mock.assert_called_once()
        self.assertEqual(
            list_mock.call_args.kwargs["filter_formula"],
            pending_schedule_filter_formula(),
        )


class AirtableClientTests(unittest.TestCase):
    def test_list_records_paginates(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {
                    "records": [
                        {"id": "rec1", "fields": {FIELD_TITLE: "A"}},
                    ],
                    "offset": "itr123",
                },
                {
                    "records": [
                        {"id": "rec2", "fields": {FIELD_TITLE: "B"}},
                    ],
                },
            ]
            records = client.list_records()

        self.assertEqual([record.id for record in records], ["rec1", "rec2"])
        self.assertEqual(request_mock.call_count, 2)
        self.assertEqual(request_mock.call_args_list[1].kwargs["query"]["offset"], "itr123")

    def test_update_record(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        with patch.object(client, "_request") as request_mock:
            request_mock.return_value = {
                "id": "rec1",
                "fields": {FIELD_TITLE: "Updated"},
            }
            record = client.update_record("rec1", {FIELD_TITLE: "Updated"})

        self.assertEqual(record.id, "rec1")
        self.assertEqual(record.fields[FIELD_TITLE], "Updated")
        request_mock.assert_called_once()
        self.assertEqual(request_mock.call_args.args[0], "PATCH")
        self.assertEqual(
            request_mock.call_args.kwargs["body"],
            {"fields": {FIELD_TITLE: "Updated"}},
        )

    def test_update_records_batches(self) -> None:
        client = AirtableClient("pat-test", "app123", "Catalog")
        updates = [(f"rec{i}", {FIELD_TITLE: f"Title {i}"}) for i in range(11)]

        with patch.object(client, "_request") as request_mock:
            request_mock.side_effect = [
                {"records": [{"id": f"rec{i}", "fields": {}} for i in range(10)]},
                {"records": [{"id": "rec10", "fields": {}}]},
            ]
            updated = client.update_records(updates)

        self.assertEqual(len(updated), 11)
        self.assertEqual(request_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
