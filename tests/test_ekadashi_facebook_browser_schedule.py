"""Tests for Ekadashi calendar ICS parsing + Facebook/Instagram schedule helpers."""
from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from media_publisher.ekadashi import (
    CAPTIONS_FROM_2025,
    CalendarEkadashi,
    EkadashiCalendarError,
    build_ekadashi_entries,
    ekadashi_entries_for_month,
    ekadashi_entries_for_year,
    parse_ekadashi_design_year,
    parse_ekadashi_events_from_ics,
)
from media_publisher.ekadashi_facebook_browser_schedule import (
    PreparedEkadashiScheduleItem,
    content_fingerprint,
    ekadashi_schedule_at,
    find_ekadashi_design,
    load_schedule_history,
    mark_scheduled_in_history,
    save_schedule_history,
    select_pending_schedule_items,
)
from media_publisher.sources.canva import CanvaDesignSummary

_SAMPLE_ICS = """
BEGIN:VCALENDAR

VERSION:2.0

BEGIN:VEVENT

DTSTART;VALUE=DATE:20261006

SUMMARY:Ekadashi

END:VEVENT

BEGIN:VEVENT

DTSTART;VALUE=DATE:20261022

SUMMARY:Ekadashi

END:VEVENT

BEGIN:VEVENT

DTSTART;VALUE=DATE:20261220

SUMMARY:Vaikuntha Ekadashi

END:VEVENT

BEGIN:VEVENT

DTSTART;VALUE=DATE:20250922

SUMMARY:Ekadashi

END:VEVENT

END:VCALENDAR
"""


def _year_events_2026() -> list[CalendarEkadashi]:
    """24 unique 2026 dates (two per month) for caption pairing tests."""
    events: list[CalendarEkadashi] = []
    for month in range(1, 13):
        events.append(
            CalendarEkadashi(event_date=date(2026, month, 6), summary="Ekadashi")
        )
        events.append(
            CalendarEkadashi(
                event_date=date(2026, month, 20),
                summary="Vaikuntha Ekadashi" if month == 12 else "Ekadashi",
            )
        )
    return events


class EkadashiCalendarParseTests(unittest.TestCase):
    def test_parse_all_day_ekadashi_dates(self) -> None:
        events = parse_ekadashi_events_from_ics(_SAMPLE_ICS, year=2026)
        self.assertEqual(
            [e.event_date for e in events],
            [date(2026, 10, 6), date(2026, 10, 22), date(2026, 12, 20)],
        )
        self.assertEqual(events[-1].summary, "Vaikuntha Ekadashi")

    def test_build_entries_pairs_captions_by_ordinal(self) -> None:
        entries = build_ekadashi_entries(_year_events_2026())
        self.assertEqual(len(entries), 24)
        self.assertEqual(len(entries), len(CAPTIONS_FROM_2025))
        self.assertEqual(entries[0].page_number, 1)
        self.assertEqual(entries[0].caption, CAPTIONS_FROM_2025[0])
        self.assertEqual(entries[-1].name, "Vaikuntha Ekadashi")
        self.assertEqual(entries[-1].page_number, 24)

    def test_month_filter_keeps_year_page_numbers(self) -> None:
        events = [
            CalendarEkadashi(event_date=date(2026, 9, 7), summary="Ekadashi"),
            CalendarEkadashi(event_date=date(2026, 9, 22), summary="Ekadashi"),
            CalendarEkadashi(event_date=date(2026, 10, 6), summary="Ekadashi"),
            CalendarEkadashi(event_date=date(2026, 10, 22), summary="Ekadashi"),
        ]
        captions = CAPTIONS_FROM_2025[:4]
        october = ekadashi_entries_for_month(
            2026, 10, events=events, captions=captions
        )
        self.assertEqual([e.day for e in october], [6, 22])
        self.assertEqual([e.page_number for e in october], [3, 4])

    def test_caption_count_mismatch(self) -> None:
        events = parse_ekadashi_events_from_ics(_SAMPLE_ICS, year=2026)
        with self.assertRaises(EkadashiCalendarError):
            build_ekadashi_entries(events)

    def test_schedule_at_is_day_before_1230_sofia(self) -> None:
        when = ekadashi_schedule_at(
            date(2026, 10, 6), publish_timezone="Europe/Sofia"
        )
        self.assertEqual(
            when,
            datetime(2026, 10, 5, 12, 30, tzinfo=ZoneInfo("Europe/Sofia")),
        )

    def test_parse_ekadashi_design_year(self) -> None:
        self.assertEqual(parse_ekadashi_design_year("Ekadashi 2026"), 2026)
        self.assertEqual(parse_ekadashi_design_year("ekadashi 2025"), 2025)
        self.assertIsNone(parse_ekadashi_design_year("Quotes 2026"))

    def test_find_design_falls_back_to_latest_year(self) -> None:
        client = MagicMock()
        client.list_folder_designs.return_value = (
            [
                CanvaDesignSummary(id="d25", title="Ekadashi 2025", page_count=24),
                CanvaDesignSummary(id="d26", title="Ekadashi 2026", page_count=24),
                CanvaDesignSummary(id="other", title="Something else", page_count=1),
            ],
            None,
        )
        resolved = find_ekadashi_design(client, year=2027)
        self.assertTrue(resolved.is_fallback)
        self.assertEqual(resolved.design_year, 2026)
        self.assertEqual(resolved.design_id, "d26")
        self.assertEqual(resolved.requested_year, 2027)

        exact = find_ekadashi_design(client, year=2026)
        self.assertFalse(exact.is_fallback)
        self.assertEqual(exact.design_id, "d26")


class EkadashiScheduleHistoryTests(unittest.TestCase):
    def test_history_roundtrip_and_pending_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "2026-10-06.jpg"
            image.write_bytes(b"ekadashi-bytes")
            events = [
                CalendarEkadashi(event_date=date(2026, 10, 6), summary="Ekadashi"),
                CalendarEkadashi(event_date=date(2026, 10, 22), summary="Ekadashi"),
            ]
            captions = CAPTIONS_FROM_2025[:2]
            entries = ekadashi_entries_for_year(
                2026, events=events, captions=captions
            )
            entry = entries[0]
            item = PreparedEkadashiScheduleItem(
                entry=entry,
                caption=entry.caption,
                publish_at=datetime(
                    2026, 10, 6, 8, 0, tzinfo=ZoneInfo("Europe/Sofia")
                ),
                image_path=image,
                content_fingerprint=content_fingerprint(
                    image_path=image, caption=entry.caption
                ),
                canva_design_id="design1",
            )
            history_path = root / "history.json"
            history: dict = {}
            mark_scheduled_in_history(
                history,
                stem=item.entry.stem,
                permalink="https://facebook.com/p/1",
                publish_at=item.publish_at,
                content_fingerprint=item.content_fingerprint,
                canva_design_id=item.canva_design_id,
            )
            save_schedule_history(history_path, history)
            loaded = load_schedule_history(history_path)
            now = datetime(2026, 9, 30, 20, 0, tzinfo=ZoneInfo("Europe/Sofia"))
            pending, skips = select_pending_schedule_items(
                [item], loaded, now=now, force=False
            )
            self.assertEqual(pending, [])
            self.assertEqual(skips[0].reason, "already in history")


if __name__ == "__main__":
    unittest.main()
