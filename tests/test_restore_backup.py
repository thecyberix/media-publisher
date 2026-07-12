from __future__ import annotations

import unittest

from catalog_parser.airtable import FIELD_TITLE
from catalog_parser.workflow.restore import build_restore_plan


class RestorePlanTests(unittest.TestCase):
    def test_build_restore_plan_detects_update_create_and_orphan(self) -> None:
        backup_records = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Keep me",
                    "Status": "1. To do",
                },
            },
            {
                "id": "rec2",
                "fields": {
                    FIELD_TITLE: "Recreate me",
                    "Status": "2. Translation done",
                },
            },
        ]
        live_records = [
            {
                "id": "rec1",
                "fields": {
                    FIELD_TITLE: "Keep me",
                    "Status": "3. Editing done",
                },
            },
            {
                "id": "rec3",
                "fields": {
                    FIELD_TITLE: "Delete candidate",
                    "Status": "1. To do",
                },
            },
        ]

        plan = build_restore_plan(
            backup_records,
            live_records,
            backup_fetched_at="2026-07-10T00:00:00+00:00",
        )

        self.assertEqual(len(plan.updates), 1)
        self.assertEqual(plan.updates[0].record_id, "rec1")
        self.assertEqual(plan.updates[0].fields, {"Status": "1. To do"})
        self.assertEqual(len(plan.creates), 1)
        self.assertEqual(plan.creates[0].title, "Recreate me")
        self.assertEqual(len(plan.orphans), 1)
        self.assertEqual(plan.orphans[0].record_id, "rec3")


if __name__ == "__main__":
    unittest.main()
