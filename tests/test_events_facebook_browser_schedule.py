"""Tests for local Facebook Playwright event scheduling helpers."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from media_publisher.events.facebook_browser_schedule import (
    content_fingerprint,
    history_blocks_item,
    load_schedule_history,
    mark_scheduled_in_history,
    save_schedule_history,
)
from media_publisher.sources.google_drive import local_file_md5


class EventFacebookBrowserScheduleTests(unittest.TestCase):
    def test_history_roundtrip_and_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "poster.jpg"
            image.write_bytes(b"event-bytes")
            fp = content_fingerprint(image_path=image, caption="Caption")
            history_path = root / "history.json"
            history: dict = {}
            mark_scheduled_in_history(
                history,
                dedupe_key="abc123",
                permalink="https://facebook.com/p/1",
                publish_at=datetime(
                    2026, 10, 1, 8, 0, tzinfo=ZoneInfo("Europe/Sofia")
                ),
                content_fingerprint=fp,
                drive_file_id="drive1",
                scheduled_at=datetime(
                    2026, 9, 30, 12, 0, tzinfo=ZoneInfo("Europe/Sofia")
                ),
            )
            save_schedule_history(history_path, history)
            loaded = load_schedule_history(history_path)
            self.assertEqual(
                loaded["abc123"]["permalink"], "https://facebook.com/p/1"
            )
            self.assertTrue(
                history_blocks_item(
                    loaded,
                    dedupe_key="abc123",
                    content_fingerprint=fp,
                    force=False,
                )
            )
            self.assertFalse(
                history_blocks_item(
                    loaded,
                    dedupe_key="abc123",
                    content_fingerprint=fp,
                    force=True,
                )
            )

    def test_fingerprint_change_unblocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "poster.jpg"
            image.write_bytes(b"v1")
            fp_v1 = content_fingerprint(image_path=image, caption="Same")
            history = {
                "key1": {
                    "permalink": "https://facebook.com/p/old",
                    "content_fingerprint": fp_v1,
                }
            }
            image.write_bytes(b"v2-changed")
            fp_v2 = content_fingerprint(image_path=image, caption="Same")
            self.assertNotEqual(fp_v1, fp_v2)
            self.assertFalse(
                history_blocks_item(
                    history,
                    dedupe_key="key1",
                    content_fingerprint=fp_v2,
                    force=False,
                )
            )

    def test_content_fingerprint_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.jpg"
            path.write_bytes(b"same")
            a = content_fingerprint(image_path=path, caption="Hi")
            b = content_fingerprint(image_path=path, caption="Hi")
            c = content_fingerprint(image_path=path, caption="Ho")
            self.assertEqual(a, b)
            self.assertNotEqual(a, c)
            self.assertEqual(local_file_md5(path), local_file_md5(path))

    def test_missing_permalink_does_not_block(self) -> None:
        history = {
            "key1": {"content_fingerprint": "abc"},
        }
        self.assertFalse(
            history_blocks_item(
                history,
                dedupe_key="key1",
                content_fingerprint="abc",
                force=False,
            )
        )


if __name__ == "__main__":
    unittest.main()
