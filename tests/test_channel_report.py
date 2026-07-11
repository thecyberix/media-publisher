from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from media_publisher.analytics.channel_report import (
    ChannelReportMapping,
    MonthColumn,
    PlatformSection,
    _detect_platform_columns,
    _load_platform_metric_targets,
    _parse_report_rows,
    _resolve_metric_value,
    _select_month_columns,
    load_channel_report_mapping,
    metric_key_for_label,
    normalize_metric_label,
    parse_month_cell,
    update_channel_report,
)
from media_publisher.analytics.meta_analytics import _sum_insight_values
from media_publisher.analytics.youtube_analytics import _parse_month_value
from media_publisher.sources.google_sheets import a1_cell, column_index_to_a1


class ParseMonthCellTests(unittest.TestCase):
    def test_iso_month(self) -> None:
        self.assertEqual(parse_month_cell("2024-03"), (2024, 3))

    def test_european_month(self) -> None:
        self.assertEqual(parse_month_cell("03/2024"), (2024, 3))

    def test_bulgarian_month(self) -> None:
        self.assertEqual(parse_month_cell("март 2024"), (2024, 3))

    def test_english_month(self) -> None:
        self.assertEqual(parse_month_cell("March 2025"), (2025, 3))

    def test_short_month_year(self) -> None:
        self.assertEqual(parse_month_cell("Jan/26"), (2026, 1))
        self.assertEqual(parse_month_cell("Feb/25"), (2025, 2))

    def test_invalid_month(self) -> None:
        self.assertIsNone(parse_month_cell(""))
        self.assertIsNone(parse_month_cell("notes"))


class ChannelReportMappingTests(unittest.TestCase):
    def test_load_mapping_with_platform_sections(self) -> None:
        payload = {
            "spreadsheet_id": "abc123",
            "sheet_gid": 1179708,
            "layout": "kpi_dashboard",
            "platform_sections": {
                "youtube": {"start_row": 43, "end_row": 55},
                "instagram": {"start_row": 76, "end_row": 88},
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "mapping.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            mapping = load_channel_report_mapping(path)
        self.assertEqual(mapping.platform_sections["youtube"], PlatformSection(43, 55))

    def test_metric_key_for_label(self) -> None:
        self.assertEqual(metric_key_for_label("▶️ VIDEO Views"), "video_views")
        self.assertEqual(metric_key_for_label("⌛ Watchtime"), "watch_time_hours")
        self.assertIsNone(metric_key_for_label("LAU Planned"))
        self.assertIsNone(metric_key_for_label(""))

    def test_resolve_metric_value_uses_fallback(self) -> None:
        metrics = {
            "youtube": {
                "2024-01": {"total_views": 12345.0},
            }
        }
        self.assertEqual(
            _resolve_metric_value(metrics, "youtube", "2024-01", "video_views"),
            12345.0,
        )

    def test_load_mapping_from_file(self) -> None:
        payload = {
            "spreadsheet_id": "abc123",
            "sheet_gid": 1179708,
            "layout": "kpi_dashboard",
            "platform_rows": {"youtube": 37, "instagram": 70},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "mapping.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            mapping = load_channel_report_mapping(path)
        self.assertEqual(mapping.spreadsheet_id, "abc123")
        self.assertEqual(mapping.sheet_gid, 1179708)
        self.assertEqual(mapping.platform_rows["youtube"], 37)

    def test_detect_platform_columns(self) -> None:
        mapping = ChannelReportMapping(
            spreadsheet_id="abc",
            platform_columns={
                "youtube": ["YouTube", "YT"],
                "facebook": ["Facebook", "FB"],
                "instagram": ["Instagram", "IG"],
            },
        )
        headers = ["Month", "YouTube", "Facebook", "Instagram"]
        detected = _detect_platform_columns(headers, mapping)
        self.assertEqual(detected, {"youtube": 1, "facebook": 2, "instagram": 3})


class ReportRowParsingTests(unittest.TestCase):
    def test_parse_report_rows(self) -> None:
        mapping = ChannelReportMapping(
            spreadsheet_id="abc",
            first_data_row=2,
            month_column_index=0,
            platform_columns={"youtube": ["YouTube"]},
        )
        rows = [["2024-01"], ["2024-02"], ["invalid"], ["2024-03"]]
        parsed = _parse_report_rows(rows, mapping)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(parsed[0].row_number, 2)
        self.assertEqual((parsed[0].year, parsed[0].month), (2024, 1))


class GoogleSheetsHelperTests(unittest.TestCase):
    def test_column_index_to_a1(self) -> None:
        self.assertEqual(column_index_to_a1(0), "A")
        self.assertEqual(column_index_to_a1(25), "Z")
        self.assertEqual(column_index_to_a1(26), "AA")

    def test_a1_cell(self) -> None:
        self.assertEqual(a1_cell("Bulgarian", 5, 2), "'Bulgarian'!C5")


class YouTubeAnalyticsParsingTests(unittest.TestCase):
    def test_parse_month_value(self) -> None:
        self.assertEqual(_parse_month_value("2024-05"), (2024, 5))
        self.assertIsNone(_parse_month_value("2024/05"))


class MetaAnalyticsParsingTests(unittest.TestCase):
    def test_sum_insight_values_prefers_total_value(self) -> None:
        payload = {
            "data": [
                {
                    "total_value": {"value": 42},
                    "values": [{"value": 10}, {"value": 20}],
                }
            ]
        }
        self.assertEqual(_sum_insight_values(payload), 42)

    def test_sum_insight_values_sums_daily_values(self) -> None:
        payload = {
            "data": [
                {
                    "values": [{"value": 10}, {"value": 20}],
                }
            ]
        }
        self.assertEqual(_sum_insight_values(payload), 30)


class KpiDashboardTests(unittest.TestCase):
    def test_select_last_complete_month_only(self) -> None:
        columns = [
            MonthColumn(year=2026, month=1, column_index=6),
            MonthColumn(year=2026, month=2, column_index=7),
            MonthColumn(year=2026, month=3, column_index=8),
        ]
        selected = _select_month_columns(
            columns,
            complete_through=date(2026, 2, 1),
            target_month=None,
            all_months=False,
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual((selected[0].year, selected[0].month), (2026, 2))

    def test_load_metric_rows_from_sections_only(self) -> None:
        mapping = ChannelReportMapping(
            spreadsheet_id="abc",
            sheet_title="Bulgarian",
            platform_sections={"youtube": PlatformSection(start_row=43, end_row=44)},
        )
        sheets = MagicMock()
        sheets.get_values.return_value = [
            ["", "", "", "", "", "▶️ VIDEO Views"],
        ]
        targets = _load_platform_metric_targets(sheets, mapping, "Bulgarian")
        self.assertEqual([item.row_number for item in targets["youtube"]], [43])

    def test_kpi_dashboard_dry_run(self) -> None:
        mapping = ChannelReportMapping(
            spreadsheet_id="abc",
            sheet_title="Bulgarian",
            layout="kpi_dashboard",
            month_header_row=3,
            platform_sections={
                "youtube": PlatformSection(start_row=43, end_row=55),
                "instagram": PlatformSection(start_row=76, end_row=88),
            },
        )
        sheets = MagicMock()
        sheets.resolve_sheet_title.return_value = "Bulgarian"
        sheets.get_values.side_effect = [
            [["", "", "", "", "", "", "Jan/26", "Feb/26"]],
            [
                ["", "", "", "", "", "▶️ VIDEO Views"],
                ["", "", "", "", "", "Shorts Views"],
            ],
            [
                ["", "", "", "", "", "▶️ VIDEO Views"],
                ["", "", "", "", "", "Shorts Views"],
            ],
        ]

        youtube_client = MagicMock()
        meta_client = MagicMock()

        with unittest.mock.patch(
            "media_publisher.analytics.channel_report.fetch_youtube_monthly_metrics_for_client",
            return_value={"2026-02": {"video_views": 183000.0, "shorts_views": 162000.0}},
        ), unittest.mock.patch(
            "media_publisher.analytics.channel_report.fetch_instagram_monthly_metrics",
            return_value={"2026-02": {"video_views": 137000.0, "shorts_views": 117000.0}},
        ), unittest.mock.patch(
            "media_publisher.analytics.channel_report.last_complete_month",
            return_value=date(2026, 2, 1),
        ):
            result = update_channel_report(
                mapping=mapping,
                sheets_client=sheets,
                youtube_client=youtube_client,
                youtube_channel_id="UC123",
                meta_client=meta_client,
                meta_page_id="page123",
                meta_instagram_account_id="ig123",
                dry_run=True,
            )

        self.assertEqual(len(result.updates), 4)
        self.assertEqual(result.updates[0].cell, "'Bulgarian'!H43")
        self.assertEqual(result.updates[2].cell, "'Bulgarian'!H76")
        sheets.batch_update_values.assert_not_called()


class UpdateChannelReportTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self) -> None:
        mapping = ChannelReportMapping(
            spreadsheet_id="abc",
            sheet_title="Bulgarian",
            layout="month_rows",
            header_row=1,
            first_data_row=2,
            month_column_index=0,
            platform_columns={
                "youtube": ["YouTube"],
                "facebook": ["Facebook"],
                "instagram": ["Instagram"],
            },
        )
        sheets = MagicMock()
        sheets.resolve_sheet_title.return_value = "Bulgarian"
        sheets.get_values.side_effect = [
            [["Month", "YouTube", "Facebook", "Instagram"]],
            [["2024-01"], ["2024-02"]],
        ]

        youtube_client = MagicMock()
        youtube_client.ensure_access_token.return_value = "token"

        meta_client = MagicMock()

        with unittest.mock.patch(
            "media_publisher.analytics.channel_report.fetch_youtube_monthly_metrics_for_client",
            return_value={
                "2024-01": {"video_views": 100.0},
                "2024-02": {"video_views": 200.0},
            },
        ), unittest.mock.patch(
            "media_publisher.analytics.channel_report.fetch_facebook_monthly_metrics",
            return_value={
                "2024-01": {"video_views": 50.0},
                "2024-02": {"video_views": 60.0},
            },
        ), unittest.mock.patch(
            "media_publisher.analytics.channel_report.fetch_instagram_monthly_metrics",
            return_value={
                "2024-01": {"video_views": 70.0},
                "2024-02": {"video_views": 80.0},
            },
        ), unittest.mock.patch(
            "media_publisher.analytics.channel_report.last_complete_month",
            return_value=date(2099, 12, 1),
        ):
            result = update_channel_report(
                mapping=mapping,
                sheets_client=sheets,
                youtube_client=youtube_client,
                youtube_channel_id="UC123",
                meta_client=meta_client,
                meta_page_id="page123",
                meta_instagram_account_id="ig123",
                dry_run=True,
                all_months=True,
            )

        self.assertEqual(len(result.updates), 6)
        sheets.batch_update_values.assert_not_called()


if __name__ == "__main__":
    unittest.main()
