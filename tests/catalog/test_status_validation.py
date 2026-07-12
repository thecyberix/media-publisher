from __future__ import annotations

import unittest

from catalog_parser.airtable import (
    FIELD_ORIGINAL_VIDEO_DESCRIPTION,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_DESCRIPTION_TRANSLATED,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.workflow.status_validation import (
    MISSING_CAPTION_COMMENT,
    MISSING_DESCRIPTION_COMMENT,
    MISSING_TITLE_COMMENT,
    detect_invalid_status_transitions,
    missing_translation_requirements,
)


class StatusValidationTests(unittest.TestCase):
    def test_missing_requirements_all_conditional(self) -> None:
        fields = {
            FIELD_ORIGINAL_VIDEO_DESCRIPTION: "Original text",
            FIELD_ORIGINAL_VIDEO_THUMBNAIL: [{"url": "https://example/thumb.jpg"}],
        }
        missing = missing_translation_requirements(fields)
        self.assertEqual(
            missing,
            [
                MISSING_TITLE_COMMENT,
                MISSING_DESCRIPTION_COMMENT,
                MISSING_CAPTION_COMMENT,
            ],
        )

    def test_missing_requirements_skip_description_when_original_empty(self) -> None:
        fields = {
            FIELD_VIDEO_NAME_TRANSLATED: "Translated title",
            FIELD_ORIGINAL_VIDEO_DESCRIPTION: "",
        }
        self.assertEqual(missing_translation_requirements(fields), [])

    def test_detect_invalid_translation_done_transition(self) -> None:
        previous = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    "Type": "Video",
                    FIELD_STATUS: STATUS_TODO,
                },
            }
        ]
        current = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    "Type": "Video",
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                },
            }
        ]
        actions = detect_invalid_status_transitions(previous, current)
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].previous_status, STATUS_TODO)
        self.assertEqual(actions[0].attempted_status, STATUS_TRANSLATION_DONE)
        self.assertIn(MISSING_TITLE_COMMENT, actions[0].comments)

    def test_detect_allows_valid_editing_done_transition(self) -> None:
        previous = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    "Type": "Reel",
                    FIELD_STATUS: STATUS_TRANSLATION_DONE,
                    FIELD_ORIGINAL_VIDEO_DESCRIPTION: "Original",
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL: [{"url": "https://example/thumb.jpg"}],
                },
            }
        ]
        current = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Sample",
                    "Type": "Reel",
                    FIELD_STATUS: STATUS_EDITING_DONE,
                    FIELD_VIDEO_NAME_TRANSLATED: "Title",
                    FIELD_VIDEO_DESCRIPTION_TRANSLATED: "Description",
                    FIELD_VIDEO_CAPTION_TRANSLATED: "Caption",
                    FIELD_ORIGINAL_VIDEO_DESCRIPTION: "Original",
                    FIELD_ORIGINAL_VIDEO_THUMBNAIL: [{"url": "https://example/thumb.jpg"}],
                },
            }
        ]
        self.assertEqual(detect_invalid_status_transitions(previous, current), [])


if __name__ == "__main__":
    unittest.main()
