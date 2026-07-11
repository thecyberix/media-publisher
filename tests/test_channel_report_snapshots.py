from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from media_publisher.analytics.channel_report_snapshots import (
    SnapshotStore,
    apply_snapshots_to_monthly_metrics,
    latest_value_for_month,
    load_snapshot_store,
    record_snapshot,
    save_snapshot_store,
)


class SnapshotStoreTests(unittest.TestCase):
    def test_record_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "snapshots.json"
            store = SnapshotStore()
            record_snapshot(
                store,
                platform="instagram",
                metric_key="followers",
                value=11395.0,
                captured_on=date(2026, 6, 15),
            )
            save_snapshot_store(path, store)
            loaded = load_snapshot_store(path)
        self.assertEqual(
            loaded.points["instagram"]["followers"]["2026-06-15"],
            11395.0,
        )

    def test_latest_value_for_month_uses_last_day_in_month(self) -> None:
        store = SnapshotStore()
        record_snapshot(
            store,
            platform="facebook",
            metric_key="followers",
            value=49800.0,
            captured_on=date(2026, 6, 10),
        )
        record_snapshot(
            store,
            platform="facebook",
            metric_key="followers",
            value=49880.0,
            captured_on=date(2026, 6, 30),
        )
        self.assertEqual(
            latest_value_for_month(
                store,
                platform="facebook",
                metric_key="followers",
                year=2026,
                month=6,
            ),
            49880.0,
        )

    def test_apply_snapshots_overrides_followers(self) -> None:
        store = SnapshotStore()
        record_snapshot(
            store,
            platform="instagram",
            metric_key="followers",
            value=12000.0,
            captured_on=date(2026, 7, 3),
        )
        metrics = {
            "instagram": {
                "2026-07": {"video_views": 1000.0, "followers": 1.0},
            },
            "facebook": {},
            "youtube": {},
        }
        apply_snapshots_to_monthly_metrics(
            metrics,
            store,
            start_month=date(2026, 7, 1),
            end_month=date(2026, 7, 1),
        )
        self.assertEqual(metrics["instagram"]["2026-07"]["followers"], 12000.0)
        self.assertEqual(metrics["instagram"]["2026-07"]["video_views"], 1000.0)


if __name__ == "__main__":
    unittest.main()
