from __future__ import annotations

import unittest

from catalog_parser.parser import (
    TYPE_REEL,
    TYPE_SHORT,
    TYPE_VIDEO,
    duration_to_type,
    filter_by_video_type,
    parse_pub_date,
    parse_video_type,
    sort_by_pub_date_newest_first,
    type_duration_bounds,
)


class VideoTypeTests(unittest.TestCase):
    def test_duration_to_type_boundaries(self) -> None:
        self.assertEqual(duration_to_type(90), TYPE_REEL)
        self.assertEqual(duration_to_type(91), TYPE_SHORT)
        self.assertEqual(duration_to_type(180), TYPE_SHORT)
        self.assertEqual(duration_to_type(181), TYPE_VIDEO)

    def test_parse_video_type_is_case_insensitive(self) -> None:
        self.assertEqual(parse_video_type("reel"), TYPE_REEL)
        self.assertEqual(parse_video_type("SHORT"), TYPE_SHORT)

    def test_type_duration_bounds(self) -> None:
        self.assertEqual(type_duration_bounds(TYPE_REEL), (0, 90))
        self.assertEqual(type_duration_bounds(TYPE_SHORT), (91, 180))
        self.assertEqual(type_duration_bounds(TYPE_VIDEO)[0], 181)

    def test_filter_by_video_type(self) -> None:
        records = [
            {"ctDuration": "50", "ctTitle": "Reel"},
            {"ctDuration": "120", "ctTitle": "Short"},
            {"ctDuration": "240", "ctTitle": "Video"},
        ]
        self.assertEqual(len(filter_by_video_type(records, TYPE_REEL)), 1)
        self.assertEqual(len(filter_by_video_type(records, TYPE_SHORT)), 1)
        self.assertEqual(len(filter_by_video_type(records, TYPE_VIDEO)), 1)

    def test_parse_pub_date_supports_sheet_format(self) -> None:
        parsed = parse_pub_date("10/07/26")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 7)
        self.assertEqual(parsed.day, 10)

    def test_sort_by_pub_date_newest_first(self) -> None:
        records = [
            {"ctTitle": "Older", "ctPubDate": "01/01/25"},
            {"ctTitle": "Newer", "ctPubDate": "10/07/26"},
            {"ctTitle": "Middle", "ctPubDate": "15/06/26"},
            {"ctTitle": "No date"},
        ]
        ordered = [record["ctTitle"] for record in sort_by_pub_date_newest_first(records)]
        self.assertEqual(ordered, ["Newer", "Middle", "Older", "No date"])


if __name__ == "__main__":
    unittest.main()
