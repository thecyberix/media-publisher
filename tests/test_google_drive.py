from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from media_publisher.sources.google_drive import (
    GoogleDriveClient,
    format_month_folder_name,
    format_year_folder_name,
    parse_day_from_background_filename,
)


class GoogleDriveQuoteBackgroundTests(unittest.TestCase):
    def test_format_year_folder_name(self) -> None:
        self.assertEqual(
            format_year_folder_name("SQ Photos {year}", year=2026),
            "SQ Photos 2026",
        )

    def test_format_month_folder_name(self) -> None:
        self.assertEqual(
            format_month_folder_name("{month:02d} {month_abbr} {year}", year=2026, month=7),
            "07 Jul 2026",
        )

    def test_parse_day_from_background_filename(self) -> None:
        self.assertEqual(
            parse_day_from_background_filename("Jul-1-20070512_SHA_0063-ot-e.jpg"),
            1,
        )
        self.assertEqual(
            parse_day_from_background_filename("Jul-15-20100728_JAD_0160-ot-e.jpg"),
            15,
        )
        self.assertIsNone(parse_day_from_background_filename("README.txt"))


class GoogleDriveExecuteRetryTests(unittest.TestCase):
    def test_execute_retries_transient_http_error(self) -> None:
        class _Resp:
            status = 500

        class _HttpError(Exception):
            def __init__(self) -> None:
                super().__init__("Internal Error")
                self.resp = _Resp()

        request = MagicMock()
        request.execute.side_effect = [_HttpError(), {"files": []}]
        client = GoogleDriveClient(MagicMock())

        with patch("media_publisher.transient_retry.time.sleep", return_value=None):
            result = client._execute(request)

        self.assertEqual(result, {"files": []})
        self.assertEqual(request.execute.call_count, 2)

    def test_list_children_uses_execute_helper(self) -> None:
        drive = MagicMock()
        listed = MagicMock()
        drive.files.return_value.list.return_value = listed
        client = GoogleDriveClient(drive)
        with patch.object(client, "_execute", return_value={"files": []}) as execute:
            client.list_children("folder123")
        execute.assert_called_once_with(listed)


if __name__ == "__main__":
    unittest.main()
