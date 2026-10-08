from __future__ import annotations

import unittest

from catalog_parser.parser import (
    TYPE_REEL,
    TYPE_SHORT,
    TYPE_VIDEO,
    apply_type_column_duration,
    catalog_duration,
    duration_to_type,
    filter_by_pkg_tn,
    filter_by_video_type,
    order_pkg_tn_first,
    parse_pub_date,
    parse_type_column_duration,
    parse_video_type,
    sort_by_pub_date_newest_first,
    tn_is_marked,
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

    def test_parse_type_column_duration(self) -> None:
        self.assertEqual(parse_type_column_duration("Video\n(4:08)"), 248)
        self.assertEqual(parse_type_column_duration("Reel\n(0:36)"), 36)
        self.assertEqual(parse_type_column_duration("Video\n(1:38:30)"), 5910)
        self.assertIsNone(parse_type_column_duration("Reel\n(:)"))
        self.assertIsNone(parse_type_column_duration("Video"))
        self.assertIsNone(parse_type_column_duration(None))

    def test_catalog_duration_prefers_column_h(self) -> None:
        record = {"ctType": "Video\n(4:08)", "ctDuration": ""}
        self.assertEqual(catalog_duration(record), 248)
        self.assertEqual(
            catalog_duration({"ctType": "Video\n(4:08)", "ctDuration": "10"}),
            248,
        )
        self.assertEqual(catalog_duration({"ctDuration": "240"}), 240)

    def test_missing_duration_column_still_classifies_as_video(self) -> None:
        records = apply_type_column_duration(
            [
                {
                    "ctTitle": "Why Your Body Must Align With the Sun | Sadhguru on Surya Kriya",
                    "ctType": "Video\n(4:08)",
                    "ctDuration": None,
                }
            ]
        )
        self.assertEqual(records[0]["ctDuration"], 248)
        matched = filter_by_video_type(records, TYPE_VIDEO)
        self.assertEqual([row["ctTitle"] for row in matched], [records[0]["ctTitle"]])

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


class PkgTnFilterTests(unittest.TestCase):
    def test_tn_is_marked(self) -> None:
        self.assertFalse(tn_is_marked(None))
        self.assertFalse(tn_is_marked(""))
        self.assertFalse(tn_is_marked("X"))
        self.assertFalse(tn_is_marked("x"))
        self.assertTrue(tn_is_marked("1"))
        self.assertTrue(tn_is_marked("yes"))

    def test_filter_by_pkg_tn(self) -> None:
        records = [
            {"ctTitle": "A", "pkgTn": "X"},
            {"ctTitle": "B", "pkgTn": None},
            {"ctTitle": "C", "pkgTn": "marked"},
        ]
        self.assertEqual(len(filter_by_pkg_tn(records)), 3)
        marked = filter_by_pkg_tn(records, require_marked=True)
        self.assertEqual([r["ctTitle"] for r in marked], ["C"])

    def test_order_pkg_tn_first_preserves_relative_order(self) -> None:
        records = [
            {"ctTitle": "Unmarked new", "pkgTn": "X"},
            {"ctTitle": "Marked old", "pkgTn": "1"},
            {"ctTitle": "Unmarked old", "pkgTn": None},
            {"ctTitle": "Marked new", "pkgTn": "yes"},
        ]
        ordered = [row["ctTitle"] for row in order_pkg_tn_first(records)]
        self.assertEqual(
            ordered,
            ["Marked old", "Marked new", "Unmarked new", "Unmarked old"],
        )


if __name__ == "__main__":
    unittest.main()
