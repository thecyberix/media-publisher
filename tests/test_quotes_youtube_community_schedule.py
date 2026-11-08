"""Tests for scheduling prepared quotes as YouTube community posts."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from media_publisher.quotes_facebook_browser_schedule import (
    PreparedQuoteScheduleItem,
    content_fingerprint,
    load_schedule_history,
)
from media_publisher.quotes_youtube_community_schedule import (
    schedule_prepared_quotes_via_browser,
)


def _item(image_path: Path) -> PreparedQuoteScheduleItem:
    caption = "Цитат #Садхгуру"
    when = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/Sofia"))
    return PreparedQuoteScheduleItem(
        year=2026,
        month=10,
        day=5,
        stem="2026-10-05",
        drive_name="2026-10-05.jpg",
        drive_file_id="file1",
        caption=caption,
        publish_at=when,
        image_path=image_path,
        content_fingerprint=content_fingerprint(image_path=image_path, caption=caption),
    )


class YouTubeCommunityScheduleTests(unittest.TestCase):
    def test_dry_run_does_not_call_the_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-05.jpg"
            image.write_bytes(b"quote")
            called = False
            lines: list[str] = []

            def publisher(*args, **kwargs):
                del args, kwargs
                nonlocal called
                called = True
                return 0, 0, []

            ok, failed, errors = schedule_prepared_quotes_via_browser(
                [_item(image)],
                channel_handle="SadhguruBulgarian",
                project_root=root,
                history={},
                history_path=root / "history.json",
                display_timezone="Europe/Sofia",
                dry_run=True,
                print_line=lines.append,
                publisher=publisher,
            )
            self.assertEqual((ok, failed, errors), (1, 0, []))
            self.assertFalse(called)
            self.assertTrue(any("Цитат" in line for line in lines))
            self.assertFalse((root / "history.json").exists())

    def test_success_writes_history_before_a_later_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-05.jpg"
            image.write_bytes(b"quote")
            item = _item(image)
            history: dict = {}
            history_path = root / "history.json"

            def publisher(posts, *, on_success, **kwargs):
                del kwargs
                on_success(posts[0], "https://www.youtube.com/post/UgkxABC")
                return 1, 0, []

            ok, failed, errors = schedule_prepared_quotes_via_browser(
                [item],
                channel_handle="SadhguruBulgarian",
                project_root=root,
                history=history,
                history_path=history_path,
                display_timezone="Europe/Sofia",
                print_line=lambda _line: None,
                publisher=publisher,
            )
            self.assertEqual((ok, failed, errors), (1, 0, []))
            loaded = load_schedule_history(history_path)
            self.assertEqual(
                loaded[item.stem]["permalink"],
                "https://www.youtube.com/post/UgkxABC",
            )
            self.assertEqual(
                loaded[item.stem]["content_fingerprint"],
                item.content_fingerprint,
            )

    def test_publisher_failure_leaves_history_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-05.jpg"
            image.write_bytes(b"quote")
            history: dict = {}
            history_path = root / "history.json"

            def publisher(posts, *, on_success, **kwargs):
                del posts, on_success, kwargs
                return 0, 1, ["2026-10-05: composer missing"]

            ok, failed, errors = schedule_prepared_quotes_via_browser(
                [_item(image)],
                channel_handle="SadhguruBulgarian",
                project_root=root,
                history=history,
                history_path=history_path,
                display_timezone="Europe/Sofia",
                print_line=lambda _line: None,
                publisher=publisher,
            )
            self.assertEqual(ok, 0)
            self.assertEqual(failed, 1)
            self.assertEqual(errors, ["2026-10-05: composer missing"])
            self.assertEqual(history, {})
            self.assertFalse(history_path.exists())


if __name__ == "__main__":
    unittest.main()
