from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from media_publisher.sources.google_sheets import (
    GoogleSheetsClient,
    GoogleSheetsError,
    _is_transient_sheets_error,
    cell_background_requests,
    text_format_clear_requests,
)


def _http_error(code: int, body: bytes = b'{"error":"boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://sheets.googleapis.com/v4/spreadsheets/x",
        code=code,
        msg="Error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


def _ok_response(payload: dict[str, object]) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    return response


class TransientSheetsErrorTests(unittest.TestCase):
    def test_timeout_and_connection_errors_are_transient(self) -> None:
        self.assertTrue(_is_transient_sheets_error(TimeoutError("timed out")))
        self.assertTrue(
            _is_transient_sheets_error(urllib.error.URLError("connection reset"))
        )

    def test_http_status_is_classified(self) -> None:
        self.assertTrue(_is_transient_sheets_error(_http_error(500)))
        self.assertTrue(_is_transient_sheets_error(_http_error(429)))
        self.assertFalse(_is_transient_sheets_error(_http_error(400)))
        self.assertFalse(_is_transient_sheets_error(_http_error(404)))


class GoogleSheetsRequestRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = GoogleSheetsClient(access_token="token")

    def test_retries_timeout_then_succeeds(self) -> None:
        with (
            patch(
                "media_publisher.sources.google_sheets.urllib.request.urlopen",
                side_effect=[TimeoutError("timed out"), _ok_response({"sheets": []})],
            ) as urlopen,
            patch("media_publisher.transient_retry.time.sleep", return_value=None),
        ):
            result = self.client.get_spreadsheet("sheet-id")

        self.assertEqual(result, {"sheets": []})
        self.assertEqual(urlopen.call_count, 2)

    def test_retries_transient_http_then_succeeds(self) -> None:
        with (
            patch(
                "media_publisher.sources.google_sheets.urllib.request.urlopen",
                side_effect=[_http_error(503), _ok_response({"spreadsheetId": "x"})],
            ) as urlopen,
            patch("media_publisher.transient_retry.time.sleep", return_value=None),
        ):
            result = self.client.get_spreadsheet("sheet-id")

        self.assertEqual(result, {"spreadsheetId": "x"})
        self.assertEqual(urlopen.call_count, 2)

    def test_batch_get_values_returns_one_block_per_range(self) -> None:
        payload = {
            "valueRanges": [
                {"range": "Aug 2022!A1:Z", "values": [["Date"], ["1 Aug"]]},
                {"range": "Sep 2022!A1:Z"},
            ]
        }
        with patch(
            "media_publisher.sources.google_sheets.urllib.request.urlopen",
            return_value=_ok_response(payload),
        ):
            rows = self.client.batch_get_values(
                "sheet-id",
                ["'Aug 2022'!A:Z", "'Sep 2022'!A:Z"],
            )
        self.assertEqual(rows[0], [["Date"], ["1 Aug"]])
        self.assertEqual(rows[1], [])

    def test_does_not_retry_permanent_http_errors(self) -> None:
        with patch(
            "media_publisher.sources.google_sheets.urllib.request.urlopen",
            side_effect=_http_error(404, b"not found"),
        ) as urlopen:
            with self.assertRaises(GoogleSheetsError) as caught:
                self.client.get_spreadsheet("sheet-id")

        self.assertIn("HTTP 404", str(caught.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_timeout_after_retries_is_sheets_error(self) -> None:
        with (
            patch(
                "media_publisher.sources.google_sheets.urllib.request.urlopen",
                side_effect=TimeoutError("timed out"),
            ) as urlopen,
            patch("media_publisher.transient_retry.time.sleep", return_value=None),
        ):
            with self.assertRaises(GoogleSheetsError) as caught:
                self.client.get_spreadsheet("sheet-id")

        self.assertEqual(str(caught.exception), "Google Sheets request timed out")
        self.assertEqual(urlopen.call_count, 3)


class QuoteCellFormatTests(unittest.TestCase):
    def test_text_format_clear_requests_target_single_cells(self) -> None:
        requests = text_format_clear_requests([(12, 5, 3)])
        self.assertEqual(len(requests), 1)
        repeat = requests[0]["repeatCell"]
        self.assertEqual(repeat["range"]["sheetId"], 12)
        self.assertEqual(repeat["range"]["startRowIndex"], 4)
        self.assertEqual(repeat["range"]["endRowIndex"], 5)
        self.assertEqual(repeat["range"]["startColumnIndex"], 3)
        self.assertEqual(repeat["fields"], "userEnteredFormat.textFormat")

    def test_cell_background_requests_set_and_clear_fill(self) -> None:
        yellow = cell_background_requests([(12, 5, 4)], {"red": 1.0, "green": 1.0, "blue": 0.0})
        self.assertEqual(len(yellow), 1)
        repeat = yellow[0]["repeatCell"]
        self.assertEqual(repeat["range"]["sheetId"], 12)
        self.assertEqual(repeat["range"]["startRowIndex"], 4)
        self.assertEqual(repeat["range"]["startColumnIndex"], 4)
        self.assertEqual(
            repeat["cell"]["userEnteredFormat"]["backgroundColor"],
            {"red": 1.0, "green": 1.0, "blue": 0.0},
        )
        self.assertEqual(repeat["fields"], "userEnteredFormat.backgroundColor")

        cleared = cell_background_requests([(12, 5, 4)], None)
        self.assertEqual(cleared[0]["repeatCell"]["cell"]["userEnteredFormat"], {})
        self.assertEqual(
            cleared[0]["repeatCell"]["fields"],
            "userEnteredFormat.backgroundColor",
        )


if __name__ == "__main__":
    unittest.main()
