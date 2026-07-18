from __future__ import annotations

import unittest

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

    def test_public_usercontent_download_url(self) -> None:
        self.assertEqual(
            GoogleDriveClient.public_usercontent_download_url("file123"),
            "https://drive.usercontent.google.com/download?id=file123&export=download",
        )


if __name__ == "__main__":
    unittest.main()
