from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from catalog_parser.airtable import (
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.parser import TYPE_REEL, TYPE_VIDEO
from catalog_parser.workflow.editing_done_thumbnails import (
    EditingDoneThumbCandidate,
    collect_editing_done_missing_prepared_thumbnails,
    detect_newly_editing_done_records,
    format_editing_done_missing_prepared_thumbnails_email,
    notify_editing_done_missing_prepared_thumbnails,
)
from catalog_parser.workflow.publish_schedule import FIELD_CANVA_DESIGN
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.tn_publish import find_tn_template_in_folder


class EditingDoneThumbnailsTests(unittest.TestCase):
    def test_detect_newly_editing_done_records(self) -> None:
        previous = [
            {
                "id": "rec_a",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "A",
                },
            },
            {
                "id": "rec_b",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: TYPE_VIDEO,
                    FIELD_TITLE: "B",
                },
            },
        ]
        current = [
            {
                "id": "rec_a",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "A",
                },
            },
            {
                "id": "rec_b",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: TYPE_VIDEO,
                    FIELD_TITLE: "B",
                },
            },
            {
                "id": "rec_c",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "C",
                },
            },
        ]
        newly = detect_newly_editing_done_records(previous, current)
        self.assertEqual([row["id"] for row in newly], ["rec_a"])

    def test_format_digest_includes_canva_or_tn_template_links(self) -> None:
        subject, body = format_editing_done_missing_prepared_thumbnails_email(
            [
                EditingDoneThumbCandidate(
                    record_id="rec1",
                    title="Stand Firmly",
                    translated="Здраво",
                    canva_design="https://www.canva.com/design/ABC",
                    tn_template=None,
                ),
                EditingDoneThumbCandidate(
                    record_id="rec2",
                    title="Another",
                    translated=None,
                    canva_design=None,
                    tn_template="https://drive.google.com/file/d/file123/view",
                ),
            ]
        )
        self.assertIn("2 video(s) need prepared thumbnail", subject)
        self.assertIn("Stand Firmly", body)
        self.assertIn("Canva design: https://www.canva.com/design/ABC", body)
        self.assertIn(
            "Drive TN template: https://drive.google.com/file/d/file123/view",
            body,
        )

    def test_find_tn_template_prefers_psd(self) -> None:
        drive = MagicMock()
        drive.list_children.return_value = [
            DriveFile(id="img1", name="photo.jpg", mime_type="image/jpeg"),
            DriveFile(id="psd1", name="template.psd", mime_type="image/vnd.adobe.photoshop"),
        ]
        found = find_tn_template_in_folder(drive, "folder1")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.id, "psd1")

    def test_notify_emails_digest_for_missing_prepared(self) -> None:
        previous = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "Needs thumb",
                },
            }
        ]
        current = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "Needs thumb",
                    FIELD_VIDEO_NAME_TRANSLATED: "Трябва",
                    FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc",
                    FIELD_CANVA_DESIGN: "https://www.canva.com/design/XYZ",
                    "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
                },
            }
        ]
        with (
            patch(
                "catalog_parser.workflow.editing_done_thumbnails.prepared_thumbnail_is_missing",
                return_value=True,
            ),
            patch(
                "media_publisher.sources.tn_publish.resolve_tn_template_drive_url",
                return_value="https://drive.google.com/file/d/tmpl/view",
            ),
            patch(
                "catalog_parser.workflow.editing_done_thumbnails.send_editing_done_missing_prepared_thumbnails_email",
                return_value=True,
            ) as send_mock,
        ):
            result = notify_editing_done_missing_prepared_thumbnails(
                project_root=Path("."),
                current_records=current,
                drive_service=object(),
                previous_records=previous,
            )
        self.assertTrue(result.emailed)
        self.assertEqual(result.missing, 1)
        send_mock.assert_called_once()
        candidates = send_mock.call_args.args[0]
        self.assertEqual(candidates[0].title, "Needs thumb")
        self.assertEqual(candidates[0].canva_design, "https://www.canva.com/design/XYZ")
        self.assertEqual(
            candidates[0].tn_template,
            "https://drive.google.com/file/d/tmpl/view",
        )

    def test_collect_skips_when_prepared_exists(self) -> None:
        records = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_TYPE: TYPE_REEL,
                    FIELD_TITLE: "Has thumb",
                    "Original Video Thumbnail": [{"url": "https://example/thumb.jpg"}],
                },
            }
        ]
        with patch(
            "catalog_parser.workflow.editing_done_thumbnails.prepared_thumbnail_is_missing",
            return_value=False,
        ):
            candidates = collect_editing_done_missing_prepared_thumbnails(
                records=records,
                drive_service=object(),
                project_root=Path("."),
            )
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
