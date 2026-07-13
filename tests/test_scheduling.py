from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from media_publisher.models import PlatformScheduleTask, PublishJob
from media_publisher.scheduling import (
    facebook_can_schedule,
    filter_ready_tasks,
    filter_staggered_tasks,
    instagram_is_due,
    is_platform_ready,
    next_catalog_publish_at,
    task_uses_immediate_publish,
)


class SchedulingTests(unittest.TestCase):
    def test_instagram_not_due_far_in_future(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(days=2)
        self.assertFalse(instagram_is_due(publish_at, now=now))

    def test_instagram_due_within_early_window(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(minutes=3)
        self.assertTrue(instagram_is_due(publish_at, now=now))

    def test_facebook_can_schedule_within_thirty_days(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(days=10)
        self.assertTrue(facebook_can_schedule(publish_at, now=now))

    def test_facebook_cannot_schedule_beyond_thirty_days(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(days=32)
        self.assertFalse(facebook_can_schedule(publish_at, now=now))

    def test_facebook_ready_before_publish_time(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(days=3)
        self.assertTrue(is_platform_ready("facebook", publish_at, now=now))

    def test_filter_ready_tasks_keeps_future_instagram(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = now + timedelta(days=1)
        tasks = [
            PlatformScheduleTask(
                platform="facebook",
                publish_at=publish_at,
                job=PublishJob(title="Launch"),
                record_id="rec1",
            ),
            PlatformScheduleTask(
                platform="instagram",
                publish_at=publish_at,
                job=PublishJob(title="Launch"),
                record_id="rec1",
            ),
        ]
        ready = filter_ready_tasks(tasks, now=now)
        self.assertEqual([task.platform for task in ready], ["facebook"])


    def test_next_catalog_publish_at_uses_today_before_hour(self) -> None:
        now = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)
        publish_at = next_catalog_publish_at(
            publish_timezone="UTC",
            publish_hour=18,
            now=now,
        )
        self.assertEqual(publish_at, datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc))

    def test_next_catalog_publish_at_uses_tomorrow_after_hour(self) -> None:
        now = datetime(2026, 7, 4, 19, 0, tzinfo=timezone.utc)
        publish_at = next_catalog_publish_at(
            publish_timezone="UTC",
            publish_hour=18,
            now=now,
        )
        self.assertEqual(publish_at, datetime(2026, 7, 5, 18, 0, tzinfo=timezone.utc))

    def test_filter_staggered_tasks_splits_platforms_by_day(self) -> None:
        from datetime import date

        publish_at_today = datetime(2026, 7, 4, 5, 0, tzinfo=timezone.utc)
        publish_at_tomorrow = datetime(2026, 7, 5, 5, 0, tzinfo=timezone.utc)
        tasks = [
            PlatformScheduleTask(
                platform="instagram",
                publish_at=publish_at_today,
                job=PublishJob(title="Today"),
                record_id="rec1",
            ),
            PlatformScheduleTask(
                platform="youtube",
                publish_at=publish_at_tomorrow,
                job=PublishJob(title="Tomorrow"),
                record_id="rec2",
            ),
            PlatformScheduleTask(
                platform="facebook",
                publish_at=publish_at_tomorrow,
                job=PublishJob(title="Tomorrow"),
                record_id="rec2",
            ),
        ]
        now = datetime(2026, 7, 4, 6, 0, tzinfo=timezone.utc)
        selected = filter_staggered_tasks(
            tasks,
            reference_date=date(2026, 7, 4),
            publish_timezone="UTC",
            now=now,
        )
        self.assertEqual(
            {task.platform for task in selected},
            {"instagram", "youtube", "facebook"},
        )

    def test_task_uses_immediate_publish_in_staggered_mode(self) -> None:
        self.assertTrue(task_uses_immediate_publish("instagram", "staggered"))
        self.assertFalse(task_uses_immediate_publish("youtube", "staggered"))


if __name__ == "__main__":
    unittest.main()
