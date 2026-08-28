from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import ANY, MagicMock, patch

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATED_SUBTITLES,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_SYNC_DONE,
    STATUS_TRANSLATION_DONE,
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

    def test_schedule_tomorrow_keeps_combined_media_file(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
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
        self.assertEqual(result.record_id, "rec-combined")
        airtable.update_record_fields.assert_called_once()
        update_fields = airtable.update_record_fields.call_args.args[1]
        self.assertNotIn(FIELD_COMBINED_MEDIA_FILE, update_fields)
        drive_service.files.assert_not_called()

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
            tn_template="https://drive.google.com/file/d/tmpl/view",
        )
        self.assertIn("2026-07-21", subject)
        self.assertIn("Hello Or Namaskar Whats Your Choice", body)
        self.assertIn("https://www.canva.com/design/ABC", body)
        self.assertIn("Drive TN template:", body)
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
                    "Video Folder": "https://drive.google.com/drive/folders/abc",
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
            "Video Folder": "https://drive.google.com/drive/folders/abc",
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
                    "media_publisher.sources.canva.canva_catalog_urls_from_client",
                    return_value=("https://www.canva.com/folder/long", "https://www.canva.com/folder/short"),
                ):
                    with patch.dict("os.environ", {"CANVA_URL": "https://www.canva.com/folder/parent"}):
                        with patch(
                            "catalog_parser.drive_thumbnail.resolve_canva_design_drive_url",
                            return_value="https://www.canva.com/design/XYZ",
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

    def test_schedule_falls_back_to_editing_done_when_no_sync_done_type(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-sync-video",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Long only",
                    FIELD_VIDEO_NAME_TRANSLATED: "Long tr",
                    FIELD_TYPE: TYPE_VIDEO,
                },
            },
            {
                "id": "rec-editing-reel",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TITLE: "Editing reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Editing tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_COMBINED_MEDIA_FILE: (
                        "https://drive.google.com/file/d/combined123/view"
                    ),
                    FIELD_TRANSLATED_SUBTITLES: (
                        "https://drive.google.com/file/d/subs123/view"
                    ),
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
        self.assertEqual(result.record_id, "rec-editing-reel")
        airtable.update_record_fields.assert_called_once()
        self.assertEqual(
            airtable.update_record_fields.call_args.args[1][FIELD_SG_YT_DATE],
            "2026-07-06",
        )

    def test_schedule_prefers_sync_done_over_editing_done(self) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-editing",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TITLE: "Editing reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Editing tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/c/view",
                    FIELD_TRANSLATED_SUBTITLES: "https://drive.google.com/file/d/s/view",
                },
            },
            {
                "id": "rec-sync",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Sync reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Sync tr",
                    FIELD_TYPE: TYPE_REEL,
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

        self.assertEqual(result.record_id, "rec-sync")

    def test_schedule_skips_editing_done_without_combined_and_subtitles(self) -> None:
        airtable = MagicMock()
        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=[
                {
                    "id": "rec-editing",
                    "createdTime": "2026-07-01T10:00:00.000Z",
                    "fields": {
                        FIELD_STATUS: STATUS_EDITING_DONE,
                        FIELD_TITLE: "Incomplete",
                        FIELD_VIDEO_NAME_TRANSLATED: "Incomplete tr",
                        FIELD_TYPE: TYPE_REEL,
                    },
                },
            ],
            drive_service=MagicMock(),
            target_date=date(2026, 7, 6),
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_schedule_noop_when_editing_done_already_pending(self) -> None:
        airtable = MagicMock()
        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=[
                {
                    "id": "rec-pending",
                    "fields": {
                        FIELD_STATUS: STATUS_EDITING_DONE,
                        FIELD_TITLE: "Already scheduled",
                        FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                        FIELD_SG_YT_DATE: "2026-07-06",
                        FIELD_TYPE: TYPE_REEL,
                    },
                },
            ],
            drive_service=MagicMock(),
            target_date=date(2026, 7, 6),
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_schedule_falls_back_to_translation_done_when_no_sync_or_editing(
        self,
    ) -> None:
        airtable = MagicMock()
        drive_service = MagicMock()
        records = [
            {
                "id": "rec-sync-video",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Long only",
                    FIELD_VIDEO_NAME_TRANSLATED: "Long tr",
                    FIELD_TYPE: TYPE_VIDEO,
                },
            },
            {
                "id": "rec-translation-reel",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Translation reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Translation tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                },
            },
        ]

        def _prepare(**kwargs: object) -> tuple[bool, str, dict]:
            fields = dict(kwargs["fields"])  # type: ignore[arg-type]
            fields[FIELD_COMBINED_MEDIA_FILE] = (
                "https://drive.google.com/file/d/combined123/view"
            )
            fields[FIELD_TRANSLATED_SUBTITLES] = (
                "https://drive.google.com/file/d/subs123/view"
            )
            return True, "generated", fields

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ), patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
            side_effect=_prepare,
        ) as prepare_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=drive_service,
                target_date=date(2026, 7, 6),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.record_id, "rec-translation-reel")
        prepare_mock.assert_called_once()
        self.assertFalse(prepare_mock.call_args.kwargs["dry_run"])
        airtable.update_record_fields.assert_called_once()
        self.assertEqual(
            airtable.update_record_fields.call_args.args[1][FIELD_SG_YT_DATE],
            "2026-07-06",
        )

    def test_schedule_translation_done_dry_run_prepares_without_writing_dates(
        self,
    ) -> None:
        airtable = MagicMock()
        records = [
            {
                "id": "rec-translation-reel",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Translation reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Translation tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ), patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
            return_value=(True, "would generate", records[0]["fields"]),
        ) as prepare_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=MagicMock(),
                target_date=date(2026, 7, 6),
                dry_run=True,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.record_id, "rec-translation-reel")
        self.assertFalse(result.applied)
        self.assertTrue(prepare_mock.call_args.kwargs["dry_run"])
        airtable.update_record_fields.assert_not_called()

    def test_schedule_prefers_editing_done_over_translation_done(self) -> None:
        airtable = MagicMock()
        records = [
            {
                "id": "rec-translation",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Translation reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Translation tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                },
            },
            {
                "id": "rec-editing",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TITLE: "Editing reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Editing tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/c/view",
                    FIELD_TRANSLATED_SUBTITLES: "https://drive.google.com/file/d/s/view",
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ), patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
        ) as prepare_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=MagicMock(),
                target_date=date(2026, 7, 6),
            )

        self.assertEqual(result.record_id, "rec-editing")
        prepare_mock.assert_not_called()

    def test_schedule_prefers_sync_done_over_translation_done(self) -> None:
        airtable = MagicMock()
        records = [
            {
                "id": "rec-translation",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Translation reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Translation tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                },
            },
            {
                "id": "rec-sync",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_SYNC_DONE,
                    FIELD_TITLE: "Sync reel",
                    FIELD_VIDEO_NAME_TRANSLATED: "Sync tr",
                    FIELD_TYPE: TYPE_REEL,
                },
            },
        ]

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ), patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
        ) as prepare_mock:
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=MagicMock(),
                target_date=date(2026, 7, 6),
            )

        self.assertEqual(result.record_id, "rec-sync")
        prepare_mock.assert_not_called()

    def test_schedule_skips_translation_done_without_folder_or_resources(self) -> None:
        airtable = MagicMock()
        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=[
                {
                    "id": "rec-no-folder",
                    "createdTime": "2026-07-01T10:00:00.000Z",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TITLE: "No folder",
                        FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                        FIELD_TYPE: TYPE_REEL,
                        FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                    },
                },
                {
                    "id": "rec-no-resources",
                    "createdTime": "2026-07-02T10:00:00.000Z",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TITLE: "No resources",
                        FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                        FIELD_TYPE: TYPE_REEL,
                        FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    },
                },
            ],
            drive_service=MagicMock(),
            target_date=date(2026, 7, 6),
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_schedule_skips_translation_done_when_prepare_fails(self) -> None:
        airtable = MagicMock()
        records = [
            {
                "id": "rec-fail",
                "createdTime": "2026-07-01T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Fail first",
                    FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
                },
            },
            {
                "id": "rec-ok",
                "createdTime": "2026-07-02T10:00:00.000Z",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TITLE: "Ok second",
                    FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg2",
                    FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/2",
                },
            },
        ]

        def _prepare(*, record_id: str, fields: dict, **_kwargs: object):
            if record_id == "rec-fail":
                return False, "mix failed", fields
            updated = dict(fields)
            updated[FIELD_COMBINED_MEDIA_FILE] = (
                "https://drive.google.com/file/d/c/view"
            )
            updated[FIELD_TRANSLATED_SUBTITLES] = (
                "https://drive.google.com/file/d/s/view"
            )
            return True, "generated", updated

        with patch(
            "catalog_parser.workflow.publish_schedule._notify_if_missing_prepared_thumbnail",
            return_value=False,
        ), patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
            side_effect=_prepare,
        ):
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=records,
                drive_service=MagicMock(),
                target_date=date(2026, 7, 6),
            )

        self.assertEqual(result.record_id, "rec-ok")
        airtable.update_record_fields.assert_called_once_with(
            "rec-ok",
            ANY,
        )

    def test_schedule_does_not_write_dates_when_all_translation_prepare_fail(
        self,
    ) -> None:
        airtable = MagicMock()
        with patch(
            "catalog_parser.workflow.publish_schedule._prepare_translation_done_media",
            return_value=(False, "mix failed", {}),
        ):
            result = schedule_tomorrow_publish(
                airtable=airtable,
                records=[
                    {
                        "id": "rec-fail",
                        "createdTime": "2026-07-01T10:00:00.000Z",
                        "fields": {
                            FIELD_STATUS: STATUS_TRANSLATION_DONE,
                            FIELD_TITLE: "Fail",
                            FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                            FIELD_TYPE: TYPE_REEL,
                            FIELD_VIDEO_FOLDER: (
                                "https://drive.google.com/drive/folders/pkg1"
                            ),
                            FIELD_TRANSLATION_RESOURCES: (
                                "https://ea.smartcat.com/editor/1"
                            ),
                        },
                    },
                ],
                drive_service=MagicMock(),
                target_date=date(2026, 7, 6),
            )
        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_schedule_noop_when_translation_done_already_pending(self) -> None:
        airtable = MagicMock()
        result = schedule_tomorrow_publish(
            airtable=airtable,
            records=[
                {
                    "id": "rec-pending",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TITLE: "Already scheduled",
                        FIELD_VIDEO_NAME_TRANSLATED: "Tr",
                        FIELD_SG_YT_DATE: "2026-07-06",
                        FIELD_TYPE: TYPE_REEL,
                    },
                },
            ],
            drive_service=MagicMock(),
            target_date=date(2026, 7, 6),
        )
        self.assertTrue(result.success)
        self.assertIsNone(result.record_id)
        airtable.update_record_fields.assert_not_called()

    def test_prepare_translation_done_skips_combine_when_files_exist(self) -> None:
        from catalog_parser.workflow.publish_schedule import (
            _prepare_translation_done_media,
        )

        fields = {
            FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/c/view",
            FIELD_TRANSLATED_SUBTITLES: "https://drive.google.com/file/d/s/view",
        }
        with patch(
            "catalog_parser.workflow.actions._combine_media",
        ) as combine_mock:
            ok, message, refreshed = _prepare_translation_done_media(
                record_id="rec1",
                fields=fields,
                airtable=MagicMock(),
                dry_run=False,
            )
        self.assertTrue(ok)
        self.assertIn("already present", message)
        self.assertIs(refreshed, fields)
        combine_mock.assert_not_called()

    def test_prepare_translation_done_generates_missing_files(self) -> None:
        from catalog_parser.workflow.actions import ActionResult
        from catalog_parser.workflow.publish_schedule import (
            _prepare_translation_done_media,
        )
        from catalog_parser.workflow.rules import WorkflowAction, WorkflowActionType

        fields = {
            FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
            FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
        }
        generated = {
            **fields,
            FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/c/view",
            FIELD_TRANSLATED_SUBTITLES: "https://drive.google.com/file/d/s/view",
        }
        airtable = MagicMock()
        airtable.get_record.return_value = {"id": "rec1", "fields": generated}
        combine_result = ActionResult(
            action=WorkflowAction(
                action_type=WorkflowActionType.COMBINE_MEDIA,
                record_id="rec1",
            ),
            success=True,
            message="combined",
        )
        with patch(
            "catalog_parser.workflow.config.load_workflow_config",
            return_value=MagicMock(),
        ), patch(
            "catalog_parser.workflow.actions._combine_media",
            return_value=combine_result,
        ) as combine_mock:
            ok, message, refreshed = _prepare_translation_done_media(
                record_id="rec1",
                fields=fields,
                airtable=airtable,
                dry_run=False,
            )
        self.assertTrue(ok)
        self.assertEqual(message, "combined")
        self.assertEqual(refreshed[FIELD_COMBINED_MEDIA_FILE], generated[FIELD_COMBINED_MEDIA_FILE])
        self.assertEqual(
            refreshed[FIELD_TRANSLATED_SUBTITLES],
            generated[FIELD_TRANSLATED_SUBTITLES],
        )
        combine_mock.assert_called_once()

    def test_prepare_translation_done_fails_if_subtitles_still_missing(self) -> None:
        from catalog_parser.workflow.actions import ActionResult
        from catalog_parser.workflow.publish_schedule import (
            _prepare_translation_done_media,
        )
        from catalog_parser.workflow.rules import WorkflowAction, WorkflowActionType

        fields = {
            FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/pkg1",
            FIELD_TRANSLATION_RESOURCES: "https://ea.smartcat.com/editor/1",
        }
        airtable = MagicMock()
        airtable.get_record.return_value = {
            "id": "rec1",
            "fields": {
                **fields,
                FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/c/view",
            },
        }
        combine_result = ActionResult(
            action=WorkflowAction(
                action_type=WorkflowActionType.COMBINE_MEDIA,
                record_id="rec1",
            ),
            success=True,
            message="Skipped aligned subtitles (no Translation resources)",
        )
        with patch(
            "catalog_parser.workflow.config.load_workflow_config",
            return_value=MagicMock(),
        ), patch(
            "catalog_parser.workflow.actions._combine_media",
            return_value=combine_result,
        ):
            ok, message, _refreshed = _prepare_translation_done_media(
                record_id="rec1",
                fields=fields,
                airtable=airtable,
                dry_run=False,
            )
        self.assertFalse(ok)
        self.assertIn("still missing", message)


if __name__ == "__main__":
    unittest.main()
