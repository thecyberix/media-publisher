from __future__ import annotations

import unittest
from datetime import datetime, timezone

from catalog_parser.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_DURATION,
    FIELD_EDITOR,
    FIELD_STATUS,
    FIELD_TITLE,
    FIELD_TRANSLATOR,
    FIELD_TYPE,
    STATUS_EDITING_DONE,
    STATUS_TODO,
    STATUS_TRANSLATION_DONE,
)
from catalog_parser.workflow.status_history import detect_status_work_events


class StatusHistoryTests(unittest.TestCase):
    def test_detects_translation_done_event(self) -> None:
        detected_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        events = detect_status_work_events(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Sample",
                        FIELD_TYPE: "Video",
                        FIELD_STATUS: STATUS_TODO,
                        FIELD_TRANSLATOR: "Genka Petrova",
                        FIELD_DURATION: 600,
                    },
                }
            ],
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Sample",
                        FIELD_TYPE: "Video",
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TRANSLATOR: "Genka Petrova",
                        FIELD_DURATION: 600,
                    },
                }
            ],
            detected_at=detected_at,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "translator")
        self.assertEqual(events[0].participant_name, "Genka Petrova")

    def test_detects_editing_done_event(self) -> None:
        detected_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        events = detect_status_work_events(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Sample",
                        FIELD_TYPE: "Reel",
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_EDITOR: "Nina Rueva",
                        FIELD_DURATION: 60,
                    },
                }
            ],
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Sample",
                        FIELD_TYPE: "Reel",
                        FIELD_STATUS: STATUS_EDITING_DONE,
                        FIELD_EDITOR: "Nina Rueva",
                        FIELD_DURATION: 60,
                    },
                }
            ],
            detected_at=detected_at,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "editor")

    def test_fast_forward_todo_to_editing_done_reports_both(self) -> None:
        detected_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        events = detect_status_work_events(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Fast Track",
                        FIELD_TYPE: "Video",
                        FIELD_STATUS: STATUS_TODO,
                        FIELD_TRANSLATOR: "Genka Petrova",
                        FIELD_EDITOR: "Nina Rueva",
                        FIELD_DURATION: 900,
                    },
                }
            ],
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_TITLE: "Fast Track",
                        FIELD_TYPE: "Video",
                        FIELD_STATUS: STATUS_EDITING_DONE,
                        FIELD_TRANSLATOR: "Genka Petrova",
                        FIELD_EDITOR: "Nina Rueva",
                        FIELD_DURATION: 900,
                    },
                }
            ],
            detected_at=detected_at,
        )
        kinds = {event.kind for event in events}
        self.assertEqual(kinds, {"translator", "editor"})

    def test_skips_translation_done_when_editor_already_set(self) -> None:
        detected_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        events = detect_status_work_events(
            [{"id": "rec1", "fields": {FIELD_STATUS: STATUS_TODO, FIELD_TYPE: "Reel"}}],
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TYPE: "Reel",
                        FIELD_EDITOR: "Nina Rueva",
                    },
                }
            ],
            detected_at=detected_at,
        )
        self.assertEqual(events, [])

    def test_skips_editing_done_when_combined_media_exists(self) -> None:
        detected_at = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        events = detect_status_work_events(
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_TYPE: "Reel",
                    },
                }
            ],
            [
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_STATUS: STATUS_EDITING_DONE,
                        FIELD_TYPE: "Reel",
                        FIELD_COMBINED_MEDIA_FILE: "https://drive.google.com/file/d/abc/view",
                    },
                }
            ],
            detected_at=detected_at,
        )
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
