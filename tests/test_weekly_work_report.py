from __future__ import annotations

import unittest
from datetime import datetime, timezone

from catalog_parser.reports.weekly_work import (
    ParticipantSummary,
    WeekRange,
    WeeklyWorkReport,
    WorkEvent,
    build_weekly_work_report,
    format_weekly_work_report_email,
    previous_calendar_week_range,
)
from catalog_parser.workflow.status_history import StatusWorkEvent, append_status_history

TZ = timezone.utc


class WeeklyWorkReportTests(unittest.TestCase):
    def test_previous_calendar_week_range_from_monday(self) -> None:
        ref = datetime(2026, 7, 13, 9, 0, tzinfo=TZ)  # Monday
        week = previous_calendar_week_range(tz=TZ, reference=ref)
        self.assertEqual(week.start, datetime(2026, 7, 6, 0, 0, tzinfo=TZ))
        self.assertEqual(week.end, datetime(2026, 7, 12, 23, 59, 59, 999999, tzinfo=TZ))

    def test_format_weekly_work_report_email(self) -> None:
        week = WeekRange(
            start=datetime(2026, 7, 6, tzinfo=TZ),
            end=datetime(2026, 7, 12, 23, 59, 59, tzinfo=TZ),
            label="6 Jul 2026 – 12 Jul 2026 (UTC+3)",
        )
        report = WeeklyWorkReport(
            week=week,
            events_scanned=100,
            translation_events=(
                WorkEvent(
                    participant_name="Genka Petrova",
                    participant_id="usr1",
                    record_id="rec1",
                    record_title="Sample Reel",
                    record_type="Reel",
                    duration_seconds=60,
                    kind="translator",
                    created_time=datetime(2026, 7, 7, 12, 0, tzinfo=TZ),
                ),
            ),
            editing_events=(
                WorkEvent(
                    participant_name="Nina Rueva",
                    participant_id="usr2",
                    record_id="rec2",
                    record_title="Sample Video",
                    record_type="Video",
                    duration_seconds=600,
                    kind="editor",
                    created_time=datetime(2026, 7, 8, 15, 30, tzinfo=TZ),
                ),
            ),
        )
        subject, body = format_weekly_work_report_email(report)
        self.assertIn("weekly report", subject)
        self.assertIn("Genka Petrova", body)
        self.assertIn("Nina Rueva", body)
        self.assertIn("TRANSLATION", body)
        self.assertIn("EDITING", body)
        self.assertIn("Sample Reel", body)

    def test_build_weekly_work_report_from_status_history(self) -> None:
        import tempfile
        from pathlib import Path

        week = WeekRange(
            start=datetime(2026, 7, 6, 0, 0, tzinfo=TZ),
            end=datetime(2026, 7, 12, 23, 59, 59, tzinfo=TZ),
            label="6 Jul 2026 – 12 Jul 2026 (UTC+3)",
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            history_path = Path(tmp_dir) / "status_history.json"
            append_status_history(
                history_path,
                [
                    StatusWorkEvent(
                        record_id="rec1",
                        record_title="Fast Track",
                        record_type="Video",
                        duration_seconds=900,
                        kind="translator",
                        participant_name="Genka Petrova",
                        from_status="1. To do",
                        to_status="3. Editing done",
                        detected_at="2026-07-08T09:00:00+00:00",
                    ),
                    StatusWorkEvent(
                        record_id="rec1",
                        record_title="Fast Track",
                        record_type="Video",
                        duration_seconds=900,
                        kind="editor",
                        participant_name="Nina Rueva",
                        from_status="1. To do",
                        to_status="3. Editing done",
                        detected_at="2026-07-08T09:00:00+00:00",
                    ),
                ],
            )
            report = build_weekly_work_report(history_path, week=week)

        self.assertEqual(len(report.translation_events), 1)
        self.assertEqual(len(report.editing_events), 1)
        self.assertEqual(report.translation_events[0].participant_name, "Genka Petrova")
        self.assertEqual(report.editing_events[0].participant_name, "Nina Rueva")


if __name__ == "__main__":
    unittest.main()
