from __future__ import annotations

import unittest
from datetime import datetime, timezone

from media_publisher.analytics.meta_analytics import (
    MAX_INSIGHT_WINDOW_SECONDS,
    _insight_windows_for_month,
)


class InsightWindowTests(unittest.TestCase):
    def test_january_splits_into_two_windows(self) -> None:
        windows = _insight_windows_for_month(2026, 1)
        self.assertEqual(len(windows), 2)
        self.assertLessEqual(windows[0][1] - windows[0][0], MAX_INSIGHT_WINDOW_SECONDS)
        self.assertLessEqual(windows[1][1] - windows[1][0], MAX_INSIGHT_WINDOW_SECONDS)
        self.assertEqual(windows[0][0], int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(windows[-1][1], int(datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()))

    def test_april_uses_two_windows_with_safe_limit(self) -> None:
        windows = _insight_windows_for_month(2026, 4)
        self.assertEqual(len(windows), 2)
        for since, until in windows:
            self.assertLessEqual(until - since, MAX_INSIGHT_WINDOW_SECONDS)

    def test_february_uses_single_window(self) -> None:
        windows = _insight_windows_for_month(2026, 2)
        self.assertEqual(len(windows), 1)
        self.assertLess(windows[0][1] - windows[0][0], MAX_INSIGHT_WINDOW_SECONDS)


if __name__ == "__main__":
    unittest.main()
