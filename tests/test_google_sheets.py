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


if __name__ == "__main__":
    unittest.main()
