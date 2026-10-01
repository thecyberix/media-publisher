"""Tests for local Facebook Playwright prepared-quote scheduling helpers."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from media_publisher.quotes_facebook_browser_schedule import (
    PreparedQuoteScheduleItem,
    content_fingerprint,
    load_schedule_history,
    mark_scheduled_in_history,
    parse_quote_drive_name,
    save_schedule_history,
    select_pending_schedule_items,
)
from media_publisher.sources.google_drive import DriveFile, local_file_md5


def _item(
    *,
    day: int = 5,
    year: int = 2026,
    month: int = 10,
    image_path: Path,
    caption: str = "Quote text #tag",
    publish_at: datetime | None = None,
    drive_file_id: str = "file1",
) -> PreparedQuoteScheduleItem:
    stem = f"{year:04d}-{month:02d}-{day:02d}"
    tz = ZoneInfo("Europe/Sofia")
    when = publish_at or datetime(year, month, day, 8, 0, tzinfo=tz)
    return PreparedQuoteScheduleItem(
        year=year,
        month=month,
        day=day,
        stem=stem,
        drive_name=f"{stem}.jpg",
        drive_file_id=drive_file_id,
        caption=caption,
        publish_at=when,
        image_path=image_path,
        content_fingerprint=content_fingerprint(
            image_path=image_path, caption=caption
        ),
    )


class QuotesFacebookBrowserScheduleTests(unittest.TestCase):
    def test_parse_quote_drive_name(self) -> None:
        self.assertEqual(
            parse_quote_drive_name("2026-10-05.jpg", year=2026, month=10),
            (5, "2026-10-05"),
        )
        self.assertIsNone(
            parse_quote_drive_name("2026-09-05.jpg", year=2026, month=10)
        )
        self.assertIsNone(parse_quote_drive_name("notes.txt", year=2026, month=10))

    def test_history_roundtrip_and_pending_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-05.jpg"
            image.write_bytes(b"quote-bytes")
            item = _item(image_path=image)
            history_path = root / "history.json"
            history: dict = {}
            mark_scheduled_in_history(
                history,
                stem=item.stem,
                permalink="https://facebook.com/p/1",
                publish_at=item.publish_at,
                content_fingerprint=item.content_fingerprint,
                drive_file_id=item.drive_file_id,
                scheduled_at=datetime(2026, 9, 30, 12, 0, tzinfo=ZoneInfo("Europe/Sofia")),
            )
            save_schedule_history(history_path, history)
            loaded = load_schedule_history(history_path)
            self.assertEqual(
                loaded[item.stem]["permalink"], "https://facebook.com/p/1"
            )

            now = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            pending, skips = select_pending_schedule_items(
                [item], loaded, now=now, force=False
            )
            self.assertEqual(pending, [])
            self.assertEqual(skips[0].reason, "already in history")

            pending_force, _ = select_pending_schedule_items(
                [item], loaded, now=now, force=True
            )
            self.assertEqual([p.stem for p in pending_force], [item.stem])

    def test_pending_includes_fingerprint_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-06.jpg"
            image.write_bytes(b"v1")
            item_v1 = _item(day=6, image_path=image, caption="Old")
            history = {
                item_v1.stem: {
                    "permalink": "https://facebook.com/p/old",
                    "content_fingerprint": item_v1.content_fingerprint,
                    "publish_at": item_v1.publish_at.isoformat(),
                }
            }
            image.write_bytes(b"v2-changed")
            item_v2 = _item(day=6, image_path=image, caption="Old")
            self.assertNotEqual(
                item_v1.content_fingerprint, item_v2.content_fingerprint
            )
            now = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            pending, skips = select_pending_schedule_items(
                [item_v2], history, now=now, force=False
            )
            self.assertEqual([p.stem for p in pending], [item_v2.stem])
            self.assertEqual(skips, [])

    def test_pending_skips_past_publish_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "2026-10-01.jpg"
            image.write_bytes(b"x")
            item = _item(
                day=1,
                image_path=image,
                publish_at=datetime(2026, 10, 1, 8, 0, tzinfo=ZoneInfo("Europe/Sofia")),
            )
            now = datetime(2026, 10, 1, 9, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            pending, skips = select_pending_schedule_items(
                [item], {}, now=now, force=False
            )
            self.assertEqual(pending, [])
            self.assertIn("too soon", skips[0].reason)

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

    def test_list_prepared_quote_files_filters_names(self) -> None:
        from media_publisher.quotes_facebook_browser_schedule import (
            list_prepared_quote_files,
        )

        drive = MagicMock()
        drive.find_child_folder.return_value = DriveFile(
            id="month1",
            name="10 Oct 2026",
            mime_type="application/vnd.google-apps.folder",
        )
        drive.list_children.return_value = [
            DriveFile(id="1", name="2026-10-01.jpg", mime_type="image/jpeg"),
            DriveFile(id="2", name="readme.txt", mime_type="text/plain"),
            DriveFile(id="3", name="2026-09-01.jpg", mime_type="image/jpeg"),
            DriveFile(
                id="4",
                name="sub",
                mime_type="application/vnd.google-apps.folder",
            ),
        ]
        files = list_prepared_quote_files(
            drive, year=2026, month=10, quotes_root_id="quotes"
        )
        self.assertEqual([f.name for f in files], ["2026-10-01.jpg"])


if __name__ == "__main__":
    unittest.main()
