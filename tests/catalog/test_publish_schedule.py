from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_SYNC_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.publish_schedule import (
    FIELD_SG_FB_DATE,
    FIELD_SG_IG_DATE,
    FIELD_SG_YT_DATE,
    instagram_schedule_excluded,
    schedule_tomorrow_publish,
)


class PublishScheduleTests(unittest.TestCase):
    def test_schedule_tomorrow_prefers_thumbnail_and_sets_dates(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-no-thumb",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "A",
                    FIELD_VIDEO_NAME_TRANSLATED: "A tr",
                    FIELD_TYPE: TYPE_REEL,
                },
            },
            {
                "id": "rec-thumb",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "B",
                    FIELD_VIDEO_NAME_TRANSLATED: "B tr",
                    FIELD_TYPE: TYPE_REEL,
                    "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
                },
            },
        ]

        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=records,
            drive_service=drive_service,
            target_date=date(2026, 7, 6),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.record_id, "rec-thumb")
        airtable.update_record_fields.assert_called_once()
        args = airtable.update_record_fields.call_args.args
        self.assertEqual(args[0], "rec-thumb")
        fields = args[1]
        self.assertEqual(fields[FIELD_SG_YT_DATE], "2026-07-06")
        self.assertEqual(fields[FIELD_SG_FB_DATE], "2026-07-06")
        self.assertEqual(fields[FIELD_SG_IG_DATE], "2026-07-06")

    def test_schedule_tomorrow_omits_instagram_for_long_videos(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-long",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Long talk",
                    FIELD_VIDEO_NAME_TRANSLATED: "Long tr",
                    FIELD_TYPE: TYPE_VIDEO,
                    "Duration": 1487,
                },
            },
        ]

        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=records,
            drive_service=drive_service,
            target_date=date(2026, 7, 11),
        )

        self.assertTrue(result.success)
        fields = airtable.update_record_fields.call_args.args[1]
        self.assertEqual(fields[FIELD_SG_YT_DATE], "2026-07-11")
        self.assertEqual(fields[FIELD_SG_FB_DATE], "2026-07-11")
        self.assertNotIn(FIELD_SG_IG_DATE, fields)

    def test_schedule_tomorrow_noop_when_pending_already_exists(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-pending",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Already scheduled",
                    FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                    FIELD_SG_YT_DATE: "2026-07-06",
                    FIELD_TYPE: TYPE_REEL,
                },
            },
        ]

        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=records,
            drive_service=drive_service,
            target_date=date(2026, 7, 6),
        )

        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_schedule_tomorrow_clears_combined_media_file(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        drive_service.files().get().execute.return_value = {
            "capabilities": {"canDelete": True, "canTrash": True},
        }
        records = [
            {
                "id": "rec-combined",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Combined",
                    FIELD_VIDEO_NAME_TRANSLATED: "Combined tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_COMBINED_MEDIA_FILE: (
                        "https://drive.google.com/file/d/combined123/view"
                    ),
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._remove_drive_file",
            return_value="deleted",
        ) as remove_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=drive_service,
                target_date=date(2026, 7, 6),
            )

        self.assertTrue(result.success)
        remove_mock.assert_called_once_with(drive_service, "combined123")
        self.assertEqual(airtable.update_record_fields.call_count, 2)
        clear_call = airtable.update_record_fields.call_args_list[1]
        self.assertEqual(clear_call.args[0], "rec-combined")
        self.assertEqual(clear_call.args[1], {FIELD_COMBINED_MEDIA_FILE: ""})

    def test_instagram_schedule_excluded(self) -> None:
        self.assertTrue(instagram_schedule_excluded({FIELD_TYPE: TYPE_VIDEO}))
        self.assertFalse(instagram_schedule_excluded({FIELD_TYPE: TYPE_REEL}))
        self.assertFalse(instagram_schedule_excluded({"Duration": 16 * 60}))
