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
    format_missing_prepared_thumbnail_email,
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

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ):
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

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ):
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
            with patch(
                "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
                return_value=False,
            ):
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

    def test_format_missing_prepared_thumbnail_email_includes_canva_link(self) -> None:
        subject, body = format_missing_prepared_thumbnail_email(
            title="Hello Or Namaskar Whats Your Choice",
            translated='"Здравейте" или "Намаскар"',
            canva_design="https://www.canva.com/design/ABC",
            target_date=date(2026, 7, 21),
        )
        self.assertIn("2026-07-21", subject)
        self.assertIn("Hello Or Namaskar Whats Your Choice", body)
        self.assertIn("https://www.canva.com/design/ABC", body)
        self.assertIn("Translated:", body)

    def test_schedule_emails_when_original_thumb_lacks_prepared(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-thumb",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Needs prepared thumb",
                    FIELD_VIDEO_NAME_TRANSLATED: "Needs tr",
                    FIELD_TYPE: TYPE_REEL,
                    "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
                    "Canva Design": "https://www.canva.com/design/XYZ",
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=True,
        ) as notify_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=drive_service,
                target_date=date(2026, 7, 6),
            )

        self.assertTrue(result.success)
        self.assertTrue(result.missing_prepared_thumbnail_notified)
        notify_mock.assert_called_once()
        kwargs = notify_mock.call_args.kwargs
        self.assertEqual(kwargs["fields"][FIELD_TITLE], "Needs prepared thumb")
        self.assertFalse(kwargs["dry_run"])

    def test_notify_sends_email_when_prepared_missing(self) -> None:
        from catalog_parser.workflow.publish_schedule import (
            _notify_if_missing_prepared_thumbnail,
        )

        fields = {
            FIELD_TITLE: "Needs prepared thumb",
            FIELD_VIDEO_NAME_TRANSLATED: "Needs tr",
            FIELD_TYPE: TYPE_REEL,
            "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
            "Canva Design": "https://www.canva.com/design/XYZ",
        }
        logs: list[str] = []

        with patch(
            "media_publisher.sources.publish_media.has_prepared_publish_thumbnail",
            return_value=False,
        ):
            with patch(
                "catalog_parser.workflow.publish_schedule._optional_canva_client",
                return_value=MagicMock(),
            ):
                with patch(
                    "catalog_parser.workflow.publish_schedule.send_missing_prepared_thumbnail_email",
                    return_value=True,
                ) as send_mock:
                    notified = _notify_if_missing_prepared_thumbnail(
                        fields=fields,
                        drive_service=MagicMock(),
                        target_date=date(2026, 7, 21),
                        dry_run=False,
                        log=logs.append,
                    )

        self.assertTrue(notified)
        send_mock.assert_called_once()
        self.assertEqual(
            send_mock.call_args.kwargs["canva_design"],
            "https://www.canva.com/design/XYZ",
        )
        self.assertTrue(any("missing" in line for line in logs))

    def test_notify_skips_when_no_original_thumbnail(self) -> None:
        from catalog_parser.workflow.publish_schedule import (
            _notify_if_missing_prepared_thumbnail,
        )

        notified = _notify_if_missing_prepared_thumbnail(
            fields={
                FIELD_TITLE: "No original",
                FIELD_TYPE: TYPE_REEL,
            },
            drive_service=MagicMock(),
            target_date=date(2026, 7, 21),
            dry_run=False,
            log=lambda _message: None,
        )
        self.assertFalse(notified)

    def test_schedule_skips_prepared_thumb_email_without_original(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-no-orig",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "No original",
                    FIELD_VIDEO_NAME_TRANSLATED: "No original tr",
                    FIELD_TYPE: TYPE_REEL,
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ) as notify_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=drive_service,
                target_date=date(2026, 7, 6),
            )

        self.assertTrue(result.success)
        self.assertFalse(result.missing_prepared_thumbnail_notified)
        notify_mock.assert_called_once()
        self.assertNotIn(
            "Original Video Thumbnail",
            notify_mock.call_args.kwargs["fields"],
        )


if __name__ == "__main__":
    unittest.main()
