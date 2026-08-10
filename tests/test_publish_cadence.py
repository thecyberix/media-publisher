from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock

from media_publisher.analytics.airtable_actuals import (
    fetch_airtable_monthly_actual_counts,
    merge_actual_counts,
)
from media_publisher.analytics.channel_report import metric_key_for_label
from media_publisher.analytics.publish_cadence import (
    apply_planned_counts,
    apply_zero_plan_cascade,
    planned_publish_counts,
)
from media_publisher.sources.airtable import (
    FIELD_SG_FB_DATE,
    FIELD_SG_FB_PUBLISHED,
    FIELD_SG_IG_DATE,
    FIELD_SG_IG_PUBLISHED,
    FIELD_SG_YT_DATE,
    FIELD_SG_YT_PUBLISHED,
    FIELD_TYPE,
    AirtableRecord,
)


class PublishCadenceTests(unittest.TestCase):
    def test_august_2026_youtube_has_five_saturdays(self) -> None:
        counts = planned_publish_counts(2026, 8, "youtube")
        self.assertEqual(counts.lau_planned, 5)
        self.assertEqual(counts.shorts_planned, 26)
        self.assertEqual(counts.carousels_planned, 0)

    def test_february_2026_facebook(self) -> None:
        counts = planned_publish_counts(2026, 2, "facebook")
        self.assertEqual(counts.lau_planned, 4)
        self.assertEqual(counts.shorts_planned, 24)

    def test_instagram_shorts_match_youtube_facebook(self) -> None:
        yt = planned_publish_counts(2026, 8, "youtube")
        fb = planned_publish_counts(2026, 8, "facebook")
        ig = planned_publish_counts(2026, 8, "instagram")
        self.assertEqual(ig.lau_planned, 0)
        self.assertEqual(ig.shorts_planned, yt.shorts_planned)
        self.assertEqual(ig.shorts_planned, fb.shorts_planned)
        self.assertEqual(ig.shorts_planned, 26)
        self.assertEqual(ig.carousels_planned, 0)

    def test_zero_plan_cascade(self) -> None:
        metrics: dict[str, dict[str, dict[str, float]]] = {
            "instagram": {},
            "youtube": {},
            "facebook": {},
        }
        apply_planned_counts(
            metrics,
            start_month_year=2026,
            start_month=8,
            end_month_year=2026,
            end_month=8,
        )
        metrics["instagram"]["2026-08"]["lau_views"] = 999.0
        metrics["youtube"]["2026-08"]["carousels_views"] = 42.0
        apply_zero_plan_cascade(metrics)
        self.assertEqual(metrics["instagram"]["2026-08"]["lau_actual"], 0.0)
        self.assertEqual(metrics["instagram"]["2026-08"]["lau_views"], 0.0)
        self.assertEqual(metrics["youtube"]["2026-08"]["carousels_actual"], 0.0)
        self.assertEqual(metrics["youtube"]["2026-08"]["carousels_views"], 0.0)
        self.assertEqual(metrics["youtube"]["2026-08"]["lau_planned"], 5.0)
        self.assertNotIn("lau_actual", metrics["youtube"]["2026-08"])


class MetricLabelTests(unittest.TestCase):
    def test_planned_and_carousel_labels(self) -> None:
        self.assertEqual(metric_key_for_label("LAU Planned"), "lau_planned")
        self.assertEqual(metric_key_for_label("Shorts Actual"), "shorts_actual")
        self.assertEqual(
            metric_key_for_label("Carousels Planned (IG/FB)"), "carousels_planned"
        )
        self.assertEqual(
            metric_key_for_label("Carousels Views (IG)"), "carousels_views"
        )


class AirtableActualsTests(unittest.TestCase):
    def test_counts_lau_and_shorts_per_platform(self) -> None:
        client = MagicMock()
        client.list_records.return_value = [
            AirtableRecord(
                id="rec1",
                fields={
                    FIELD_TYPE: "Video",
                    FIELD_SG_YT_DATE: "2026-07-04",
                    FIELD_SG_YT_PUBLISHED: "https://youtu.be/a",
                    FIELD_SG_FB_DATE: "2026-07-04",
                    FIELD_SG_FB_PUBLISHED: "https://facebook.com/a",
                },
            ),
            AirtableRecord(
                id="rec2",
                fields={
                    FIELD_TYPE: "Reel",
                    FIELD_SG_YT_DATE: "2026-07-05",
                    FIELD_SG_YT_PUBLISHED: "https://youtu.be/b",
                    FIELD_SG_IG_DATE: "2026-07-05",
                    FIELD_SG_IG_PUBLISHED: "https://instagram.com/b",
                },
            ),
            AirtableRecord(
                id="rec3",
                fields={
                    FIELD_TYPE: "Video",
                    FIELD_SG_YT_DATE: "2026-07-11",
                    # Missing published permalink → ignored
                },
            ),
        ]

        metrics = fetch_airtable_monthly_actual_counts(
            client,
            start_month=date(2026, 7, 1),
            end_month=date(2026, 7, 1),
        )
        self.assertEqual(metrics["youtube"]["2026-07"]["lau_actual"], 1.0)
        self.assertEqual(metrics["youtube"]["2026-07"]["shorts_actual"], 1.0)
        self.assertEqual(metrics["facebook"]["2026-07"]["lau_actual"], 1.0)
        self.assertEqual(metrics["facebook"]["2026-07"]["shorts_actual"], 0.0)
        self.assertEqual(metrics["instagram"]["2026-07"]["lau_actual"], 0.0)
        self.assertEqual(metrics["instagram"]["2026-07"]["shorts_actual"], 1.0)

    def test_merge_actual_counts_overwrites(self) -> None:
        target = {"youtube": {"2026-07": {"lau_views": 100.0}}}
        source = {
            "youtube": {
                "2026-07": {"lau_actual": 4.0, "shorts_actual": 27.0},
            }
        }
        merge_actual_counts(target, source)
        self.assertEqual(target["youtube"]["2026-07"]["lau_actual"], 4.0)
        self.assertEqual(target["youtube"]["2026-07"]["shorts_actual"], 27.0)
        self.assertEqual(target["youtube"]["2026-07"]["lau_views"], 100.0)


if __name__ == "__main__":
    unittest.main()
