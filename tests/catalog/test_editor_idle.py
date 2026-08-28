from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from catalog_parser.airtable import FIELD_EDITOR, FIELD_STATUS, STATUS_TRANSLATION_DONE
from catalog_parser.workflow.editor_idle import (
    load_editor_last_assigned,
    mark_editor_assigned,
    save_editor_last_assigned,
    seed_editor_last_assigned,
)


class EditorLastAssignedTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "editor_last_assigned.json"
            save_editor_last_assigned(
                path,
                {"Nina Rueva": date(2026, 8, 20), "Dilyana Hayes": date(2026, 8, 1)},
            )
            loaded = load_editor_last_assigned(path)
        self.assertEqual(
            loaded,
            {
                "Nina Rueva": date(2026, 8, 20),
                "Dilyana Hayes": date(2026, 8, 1),
            },
        )

    def test_mark_editor_assigned_keeps_later_date(self) -> None:
        assigned = {"Nina Rueva": date(2026, 8, 20)}
        mark_editor_assigned(assigned, "Nina Rueva", when=date(2026, 8, 10))
        self.assertEqual(assigned["Nina Rueva"], date(2026, 8, 20))
        mark_editor_assigned(assigned, "Nina Rueva", when=date(2026, 8, 21))
        self.assertEqual(assigned["Nina Rueva"], date(2026, 8, 21))

    def test_seed_does_not_start_clock_for_unknown_idle_editor(self) -> None:
        assigned: dict[str, date] = {}
        seed_editor_last_assigned(
            assigned,
            records=[],
            previous_records=None,
            editor_names=["Nina Rueva"],
            today=date(2026, 8, 28),
        )
        self.assertNotIn("Nina Rueva", assigned)

    def test_seed_marks_unknown_editor_who_currently_has_work(self) -> None:
        assigned: dict[str, date] = {}
        seed_editor_last_assigned(
            assigned,
            records=[
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_EDITOR: "Nina Rueva",
                    },
                }
            ],
            previous_records=None,
            editor_names=["Nina Rueva"],
            today=date(2026, 8, 28),
        )
        self.assertEqual(assigned["Nina Rueva"], date(2026, 8, 28))

    def test_seed_marks_editor_when_field_newly_set(self) -> None:
        assigned: dict[str, date] = {}
        seed_editor_last_assigned(
            assigned,
            records=[
                {
                    "id": "rec1",
                    "fields": {
                        FIELD_STATUS: STATUS_TRANSLATION_DONE,
                        FIELD_EDITOR: "Nina Rueva",
                    },
                }
            ],
            previous_records=[
                {
                    "id": "rec1",
                    "fields": {FIELD_STATUS: STATUS_TRANSLATION_DONE},
                }
            ],
            editor_names=["Nina Rueva"],
            today=date(2026, 8, 28),
        )
        self.assertEqual(assigned["Nina Rueva"], date(2026, 8, 28))


if __name__ == "__main__":
    unittest.main()
