from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_TITLE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
)
from media_publisher.sources.google_drive import DriveFile
from media_publisher.sources.thumbnail_review import (
    format_review_email,
    parse_background_review_decision,
    process_approved_review_thumbnails,
    process_pending_review_thumbnails,
    review_drive_filename,
    sanitize_review_stem,
    title_from_review_filename,
    thumbnail_matches_reference_aspect,
)
from PIL import Image


class ThumbnailReviewTests(unittest.TestCase):
    def test_review_filename_roundtrip(self) -> None:
        title = "Sample | Video"
        filename = review_drive_filename(title)
        self.assertTrue(filename.endswith(".review.jpg"))
        stem = title_from_review_filename(filename)
        self.assertEqual(stem, sanitize_review_stem(title))

    def test_title_from_review_filename_rejects_non_review(self) -> None:
        self.assertIsNone(title_from_review_filename("sample.jpg"))

    def test_thumbnail_matches_reference_aspect(self) -> None:
        image = Image.new("RGB", (1920, 1080))
        self.assertTrue(
            thumbnail_matches_reference_aspect(
                image,
                reference_width=1280,
                reference_height=720,
            )
        )
        portrait = Image.new("RGB", (1080, 1920))
        self.assertFalse(
            thumbnail_matches_reference_aspect(
                portrait,
                reference_width=1280,
                reference_height=720,
            )
        )

    def test_format_review_email(self) -> None:
        from media_publisher.sources.thumbnail_review import ReviewQueueItem

        subject, body = format_review_email(
            [
                ReviewQueueItem(
                    record_id="rec1",
                    title="Sample Video",
                    local_path=Path("sample.review.jpg"),
                    reason="different background",
                )
            ]
        )
        self.assertIn("1 video", subject)
        self.assertIn("Sample Video", body)
        self.assertIn("Approved", body)
        self.assertIn("Rejected", body)
        self.assertIn("informational", body)
        self.assertIn("auto-approves", body)

    def test_parse_background_review_decision(self) -> None:
        decision, reason = parse_background_review_decision(
            '{"decision": "approve", "reason": "headline at top"}'
        )
        self.assertEqual(decision, "approve")
        self.assertEqual(reason, "headline at top")
        decision, _reason = parse_background_review_decision(
            "```json\n{\"decision\": \"reject\", \"reason\": \"captions only\"}\n```"
        )
        self.assertEqual(decision, "reject")
        decision, _reason = parse_background_review_decision("[]")
        self.assertEqual(decision, "placeholder")

    def test_process_pending_moves_approve_reject_and_empty(self) -> None:
        drive = MagicMock()
        drive.list_children.return_value = [
            DriveFile(id="a1", name="Titled.review.jpg", mime_type="image/jpeg"),
            DriveFile(id="r1", name="Captions.review.jpg", mime_type="image/jpeg"),
            DriveFile(id="s1", name="Empty.review.jpg", mime_type="image/jpeg"),
            DriveFile(id="p1", name="Canva.review.jpg", mime_type="image/jpeg"),
            DriveFile(id="x1", name="ignore.jpg", mime_type="image/jpeg"),
        ]
        drive.find_child_folder.return_value = SimpleNamespace(id="rejected-id")
        drive.download_file.side_effect = lambda _fid, dest: dest.write_bytes(b"x")
        drive.ensure_folder.side_effect = lambda _parent, name: SimpleNamespace(
            id=f"{name.casefold()}-id"
        )

        def classify(path: Path) -> tuple[str, str]:
            name = path.name
            if name.startswith("Titled"):
                return "approve", "has title"
            if name.startswith("Captions"):
                return "reject", "subtitles only"
            if name.startswith("Empty"):
                return "empty", "no overlay text"
            return "placeholder", "canva download placeholder"

        results = process_pending_review_thumbnails(
            drive,
            review_folder_id="review-id",
            apply=True,
            classify=classify,
        )

        by_file = {item.drive_file: item for item in results}
        self.assertEqual(len(results), 4)
        self.assertEqual(by_file["Titled.review.jpg"].action, "moved-approved")
        self.assertEqual(by_file["Captions.review.jpg"].action, "moved-rejected")
        self.assertEqual(by_file["Empty.review.jpg"].action, "moved-rejected")
        self.assertEqual(by_file["Canva.review.jpg"].action, "kept")
        drive.move_file.assert_any_call("a1", "approved-id")
        drive.move_file.assert_any_call("r1", "rejected-id")
        drive.move_file.assert_any_call("s1", "rejected-id")
        self.assertEqual(drive.move_file.call_count, 3)
        drive.find_child_folder.assert_called_with("review-id", "Rejected")
        drive.ensure_folder.assert_called_once_with("review-id", "Approved")

    def test_process_pending_uploads_to_airtable_on_approve(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.drive_service = object()
        drive.list_children.return_value = [
            DriveFile(id="a1", name="Sample Video.review.jpg", mime_type="image/jpeg"),
        ]
        drive.find_child_folder.return_value = SimpleNamespace(id="rejected-id")
        drive.download_file.side_effect = lambda _fid, dest: dest.write_bytes(b"x")
        drive.ensure_folder.side_effect = lambda _parent, name: SimpleNamespace(
            id=f"{name.casefold()}-id"
        )
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc",
            },
        )

        with patch(
            "media_publisher.sources.thumbnail_review._translate_caption_for_approved_thumbnail",
            return_value=("translated", "source=thumbnail"),
        ):
            results = process_pending_review_thumbnails(
                drive,
                review_folder_id="review-id",
                apply=True,
                classify=lambda _path: ("approve", "has title"),
                airtable=airtable,
                records=[record],
                project_root=Path("."),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "uploaded-approved")
        self.assertEqual(results[0].caption_action, "translated")
        airtable.upload_attachment.assert_called_once()
        drive.move_file.assert_called_once_with("a1", "approved-id")
        drive.remove_file.assert_not_called()

    def test_process_pending_skips_rejected_moves_when_folder_missing(self) -> None:
        drive = MagicMock()
        drive.list_children.return_value = [
            DriveFile(id="r1", name="Captions.review.jpg", mime_type="image/jpeg"),
        ]
        drive.find_child_folder.return_value = None
        drive.download_file.side_effect = lambda _fid, dest: dest.write_bytes(b"x")

        results = process_pending_review_thumbnails(
            drive,
            review_folder_id="review-id",
            apply=True,
            classify=lambda _path: ("reject", "subtitles only"),
        )

        self.assertEqual(results[0].action, "skipped-rejected-folder-missing")
        drive.move_file.assert_not_called()
        drive.ensure_folder.assert_not_called()

    def test_process_approved_translates_empty_caption(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.drive_service = object()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]

        def download(_file_id: str, dest: Path) -> None:
            dest.write_bytes(b"fake-image")

        drive.download_file.side_effect = download
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_VIDEO_FOLDER: "https://drive.google.com/drive/folders/abc",
            },
        )

        def fake_translate(*_args, **_kwargs):
            return "translated", "source=thumbnail"

        with patch(
            "media_publisher.sources.thumbnail_review._translate_caption_for_approved_thumbnail",
            side_effect=fake_translate,
        ) as translate:
            results = process_approved_review_thumbnails(
                airtable,
                drive,
                [record],
                review_folder_id="review-id",
                apply=True,
                project_root=Path("."),
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].action, "uploaded-approved")
        self.assertEqual(results[0].caption_action, "translated")
        airtable.upload_attachment.assert_called_once()
        translate.assert_called_once()
        drive.remove_file.assert_not_called()

    def test_process_approved_skips_when_airtable_already_has_thumbnail(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_ORIGINAL_VIDEO_THUMBNAIL: [{"url": "https://example/thumb.jpg"}],
            },
        )

        results = process_approved_review_thumbnails(
            airtable,
            drive,
            [record],
            review_folder_id="review-id",
            apply=True,
        )

        self.assertEqual(results, [])
        airtable.upload_attachment.assert_not_called()
        drive.download_file.assert_not_called()
        drive.remove_file.assert_not_called()

    def test_process_approved_skips_caption_when_already_set(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.drive_service = object()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]
        drive.download_file.side_effect = lambda _fid, dest: dest.write_bytes(b"x")
        record = SimpleNamespace(
            id="rec1",
            fields={
                FIELD_TITLE: "Sample Video",
                FIELD_VIDEO_CAPTION_TRANSLATED: "Вече преведено",
            },
        )

        results = process_approved_review_thumbnails(
            airtable,
            drive,
            [record],
            review_folder_id="review-id",
            apply=True,
        )

        self.assertEqual(results[0].caption_action, "skipped")
        self.assertEqual(results[0].caption_detail, "caption already set")
        airtable.update_record.assert_not_called()
        airtable.upload_attachment.assert_called_once()
        args = airtable.upload_attachment.call_args
        self.assertEqual(args.args[0], "rec1")
        self.assertEqual(args.args[1], FIELD_ORIGINAL_VIDEO_THUMBNAIL)

    def test_process_approved_dry_run_skips_caption(self) -> None:
        airtable = MagicMock()
        drive = MagicMock()
        drive.find_child_folder.return_value = SimpleNamespace(id="approved-id")
        drive.list_children.return_value = [
            DriveFile(
                id="file1",
                name="Sample Video.review.jpg",
                mime_type="image/jpeg",
            )
        ]
        record = SimpleNamespace(id="rec1", fields={FIELD_TITLE: "Sample Video"})

        results = process_approved_review_thumbnails(
            airtable,
            drive,
            [record],
            review_folder_id="review-id",
            apply=False,
        )

        self.assertEqual(results[0].action, "planned-approved")
        self.assertEqual(results[0].caption_action, "skipped")
        self.assertEqual(results[0].caption_detail, "dry-run")
        airtable.upload_attachment.assert_not_called()
        drive.download_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
