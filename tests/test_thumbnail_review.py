from __future__ import annotations

import unittest
from pathlib import Path

from media_publisher.sources.thumbnail_review import (
    format_review_email,
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


if __name__ == "__main__":
    unittest.main()
