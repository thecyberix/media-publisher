from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from media_publisher.languages import LanguageConfigError, get_language
from media_publisher.pipeline import record_youtube_date_in_sm_catalog
from media_publisher.sources.google_sheets import GoogleSheetsError
from media_publisher.sources.sm_catalog import (
    STATUS_AMBIGUOUS,
    STATUS_NOT_FOUND,
    STATUS_SKIPPED,
    STATUS_WRITTEN,
    apply_plan,
    plan_publish_dates,
    record_publish_date,
    sheet_date_serial,
    try_record_publish_date,
)

HEADERS = ["ctId", "ctLink", "ctPubDate", "bgrPubDate", "hrvPubDate"]
YT_URL = "https://youtu.be/2IOqjyNfsF4"


class FakeSheets:
    """Serves a small grid through the GoogleSheetsClient read/write surface."""

    def __init__(self, rows: list[list[str]]) -> None:
        self.rows = [HEADERS, *rows]
        self.writes: list[tuple[str, list[list[object]]]] = []

    def get_values(self, spreadsheet_id: str, range_a1: str, **_kwargs) -> list[list[str]]:
        sheet_range = range_a1.split("!", 1)[1]
        if sheet_range == "1:1":
            return [self.rows[0]]
        if ":" in sheet_range:
            column = ord(sheet_range.split(":")[0]) - ord("A")
            return [
                [row[column]] if len(row) > column and row[column] else []
                for row in self.rows
            ]
        column = ord(sheet_range[0]) - ord("A")
        row_number = int(sheet_range[1:])
        row = self.rows[row_number - 1]
        return [[row[column]]] if len(row) > column and row[column] else []

    def batch_update_values(self, spreadsheet_id, updates, **_kwargs) -> None:
        self.writes.extend(updates)


def _record(client: FakeSheets, url: str | None = YT_URL):
    return record_publish_date(
        client,  # type: ignore[arg-type]
        "sheet-id",
        video_url=url,
        publish_date=date(2026, 11, 1),
        date_header="bgrPubDate",
    )


class SmCatalogDateTests(unittest.TestCase):
    def test_sheet_date_serial(self) -> None:
        self.assertEqual(sheet_date_serial(date(2026, 10, 31)), 46326)

    def test_fills_blank_language_date_for_matching_link(self) -> None:
        client = FakeSheets(
            [
                ["a", "https://youtu.be/other", "46000"],
                ["b", "https://www.youtube.com/watch?v=2IOqjyNfsF4", "46325"],
            ]
        )
        result = _record(client)
        self.assertEqual(result.status, STATUS_WRITTEN)
        self.assertEqual(result.row, 3)
        self.assertEqual(client.writes, [("'English'!D3", [[46327]])])

    def test_existing_date_is_overwritten(self) -> None:
        client = FakeSheets([["a", YT_URL, "46325", "01/10/26"]])
        result = _record(client)
        self.assertEqual(result.status, STATUS_WRITTEN)
        self.assertIn("was 01/10/26", result.message)
        self.assertEqual(client.writes, [("'English'!D2", [[46327]])])

    def test_unknown_link_is_reported(self) -> None:
        client = FakeSheets([["a", "https://youtu.be/other", "46000"]])
        self.assertEqual(_record(client).status, STATUS_NOT_FOUND)
        self.assertEqual(client.writes, [])

    def test_duplicate_rows_are_left_alone(self) -> None:
        client = FakeSheets([["a", YT_URL], ["b", YT_URL]])
        self.assertEqual(_record(client).status, STATUS_AMBIGUOUS)
        self.assertEqual(client.writes, [])

    def test_record_without_link_is_skipped(self) -> None:
        client = FakeSheets([["a", YT_URL]])
        self.assertEqual(_record(client, None).status, STATUS_SKIPPED)
        self.assertEqual(client.writes, [])

    def test_missing_language_column_raises(self) -> None:
        client = FakeSheets([["a", YT_URL]])
        client.rows[0] = ["ctId", "ctLink"]
        with self.assertRaises(GoogleSheetsError):
            _record(client)


class PlanPublishDatesTests(unittest.TestCase):
    def test_plan_classifies_and_applies_in_one_write(self) -> None:
        client = FakeSheets(
            [
                ["a", "https://youtu.be/aaaaaaaaaaa", "", "46327"],  # same
                ["b", "https://youtu.be/bbbbbbbbbbb", "", ""],  # new
                ["c", "https://youtu.be/ccccccccccc", "", "45000"],  # changed
                ["d", "https://youtu.be/ddddddddddd"],  # duplicate
                ["e", "https://youtu.be/ddddddddddd"],
            ]
        )
        day = date(2026, 11, 1)
        plan = plan_publish_dates(
            client,  # type: ignore[arg-type]
            "sheet-id",
            [
                ("https://youtu.be/aaaaaaaaaaa", day),
                ("https://youtu.be/bbbbbbbbbbb", day),
                ("https://youtu.be/ccccccccccc", day),
                ("https://youtu.be/ddddddddddd", day),
                ("https://youtu.be/zzzzzzzzzzz", day),
                (None, day),
            ],
            date_header="bgrPubDate",
        )
        self.assertEqual(
            [item.status for item in plan.items],
            ["same", "new", "changed", "ambiguous", "not_found", "skipped"],
        )
        self.assertEqual(apply_plan(client, "sheet-id", plan), 2),  # type: ignore[arg-type]
        self.assertEqual(
            client.writes,
            [("'English'!D3", [[46327]]), ("'English'!D4", [[46327]])],
        )


class TryRecordPublishDateTests(unittest.TestCase):
    def test_bulgarian_uses_bgr_column(self) -> None:
        language = get_language("bg")
        assert language is not None
        self.assertEqual(language.catalog_pub_date_header, "bgrPubDate")

    def test_failures_are_logged_not_raised(self) -> None:
        language = get_language("bg")
        assert language is not None
        logged: list[str] = []
        with patch(
            "media_publisher.sources.sm_catalog.GoogleSheetsClient.from_service_account",
            side_effect=GoogleSheetsError("Google Sheets request failed with HTTP 403"),
        ):
            result = try_record_publish_date(
                project_root=Path("."),
                service_account_path=Path(__file__),
                language=language,
                video_url=YT_URL,
                publish_date=date(2026, 11, 1),
                log=logged.append,
            )
        self.assertIsNone(result)
        self.assertEqual(len(logged), 1)
        self.assertIn("not recorded", logged[0])
        self.assertIn("Editor", logged[0])

    def test_missing_service_account_is_silent(self) -> None:
        language = get_language("bg")
        assert language is not None
        logged: list[str] = []
        result = try_record_publish_date(
            project_root=Path("."),
            service_account_path=Path("does-not-exist.json"),
            language=language,
            video_url=YT_URL,
            publish_date=date(2026, 11, 1),
            log=logged.append,
        )
        self.assertIsNone(result)
        self.assertEqual(logged, [])


PROTECTED = GoogleSheetsError(
    "Google Sheets request failed with HTTP 400: {\n  \"error\": {\n    "
    "\"message\": \"You are trying to edit a protected cell or object.\"\n  }\n}"
)


class PublishNeverFailsTests(unittest.TestCase):
    """The catalog step runs after a video is live; it must not raise."""

    def _run(self, **overrides):
        logged: list[str] = []
        kwargs = dict(
            project_root=Path("."),
            service_account_path=Path(__file__),
            publish_timezone="Europe/Sofia",
            publish_at=datetime(2026, 11, 1, 18, tzinfo=timezone.utc),
            log=logged.append,
        )
        kwargs.update(overrides)
        record_youtube_date_in_sm_catalog({"Original Video": YT_URL}, **kwargs)
        return logged

    def test_protected_cell_error_is_logged_with_hint(self) -> None:
        with patch(
            "media_publisher.sources.sm_catalog.GoogleSheetsClient.from_service_account",
            return_value=object(),
        ), patch(
            "media_publisher.sources.sm_catalog.record_publish_date",
            side_effect=PROTECTED,
        ), patch(
            "catalog_parser.workflow.config.load_catalog_id", return_value="sheet"
        ):
            logged = self._run()
        self.assertEqual(len(logged), 1)
        self.assertIn("protected", logged[0])
        self.assertIn("editor", logged[0])
        self.assertNotIn("\n", logged[0])

    def test_unexpected_errors_are_swallowed(self) -> None:
        with patch(
            "media_publisher.sources.sm_catalog.GoogleSheetsClient.from_service_account",
            side_effect=RuntimeError("boom"),
        ):
            self.assertTrue(self._run())
        with patch(
            "media_publisher.pipeline.selected_language",
            side_effect=LanguageConfigError("TARGET_LANGUAGE is required"),
        ):
            self.assertTrue(self._run())

    def test_bad_timezone_is_swallowed(self) -> None:
        self.assertTrue(self._run(publish_timezone="Not/AZone"))
        self.assertTrue(self._run(publish_timezone="Not/AZone", immediate=True))

    def test_missing_timezone_falls_back_to_default(self) -> None:
        with patch(
            "media_publisher.pipeline.try_record_publish_date"
        ) as recorder:
            self._run(publish_timezone=None)
        self.assertEqual(recorder.call_args.kwargs["publish_date"], date(2026, 11, 1))

    def test_catalog_client_uses_a_short_retry_budget(self) -> None:
        language = get_language("bg")
        assert language is not None
        with patch(
            "media_publisher.sources.sm_catalog.GoogleSheetsClient.from_service_account",
            side_effect=RuntimeError("stop"),
        ) as factory:
            try_record_publish_date(
                project_root=Path("."),
                service_account_path=Path(__file__),
                language=language,
                video_url=YT_URL,
                publish_date=date(2026, 11, 1),
                log=lambda _line: None,
            )
        self.assertEqual(factory.call_args.kwargs["retry_attempts"], 2)
        self.assertLessEqual(factory.call_args.kwargs["request_timeout"], 30)


if __name__ == "__main__":
    unittest.main()
