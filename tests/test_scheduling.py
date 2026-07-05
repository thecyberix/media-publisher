from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from media_publisher.models import PlatformScheduleTask, PublishJob
from media_publisher.scheduling import (
    facebook_can_schedule,
    filter_ready_tasks,
    instagram_is_due,
    is_platform_ready,
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


if __name__ == "__main__":
    unittest.main()
