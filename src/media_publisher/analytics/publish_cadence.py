from __future__ import annotations

import calendar
from dataclasses import dataclass
from typing import Literal

PlatformName = Literal["youtube", "facebook", "instagram"]

CONTENT_TYPES = ("lau", "shorts", "carousels")


@dataclass(frozen=True)
class PlannedPublishCounts:
    lau_planned: int
    shorts_planned: int
    carousels_planned: int = 0


def count_weekdays(year: int, month: int, *, weekday: int) -> int:
    """Count days in the month matching weekday (Mon=0 … Sun=6)."""
    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    return sum(1 for week in weeks if week[weekday] != 0)


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def planned_publish_counts(
    year: int,
    month: int,
    platform: PlatformName,
) -> PlannedPublishCounts:
    """Known publish cadence for the Bulgarian SM report.

    Saturday → 1 long-form (LAU) on YouTube and Facebook (Instagram: none).
    Other weekdays → 1 Short/Reel on YouTube, Facebook, and Instagram.
    Carousels are not in the cadence.
    """
    saturdays = count_weekdays(year, month, weekday=5)
    total_days = days_in_month(year, month)
    non_saturdays = total_days - saturdays

    if platform == "instagram":
        return PlannedPublishCounts(
            lau_planned=0,
            shorts_planned=non_saturdays,
            carousels_planned=0,
        )
    if platform in ("youtube", "facebook"):
        return PlannedPublishCounts(
            lau_planned=saturdays,
            shorts_planned=non_saturdays,
            carousels_planned=0,
        )
    raise ValueError(f"Unsupported platform: {platform!r}")


def apply_planned_counts(
    monthly_metrics: dict[str, dict[str, dict[str, float]]],
    *,
    start_month_year: int,
    start_month: int,
    end_month_year: int,
    end_month: int,
    platforms: tuple[PlatformName, ...] = ("youtube", "facebook", "instagram"),
) -> None:
    year = start_month_year
    month = start_month
    while (year, month) <= (end_month_year, end_month):
        key = f"{year:04d}-{month:02d}"
        for platform in platforms:
            counts = planned_publish_counts(year, month, platform)
            entry = monthly_metrics.setdefault(platform, {}).setdefault(key, {})
            entry["lau_planned"] = float(counts.lau_planned)
            entry["shorts_planned"] = float(counts.shorts_planned)
            entry["carousels_planned"] = float(counts.carousels_planned)
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1


def apply_zero_plan_cascade(
    monthly_metrics: dict[str, dict[str, dict[str, float]]],
) -> None:
    """When Planned is 0 for a content type, force matching Actual and Views to 0."""
    for platform_metrics in monthly_metrics.values():
        for values in platform_metrics.values():
            for content_type in CONTENT_TYPES:
                planned_key = f"{content_type}_planned"
                planned = values.get(planned_key)
                if planned is None or planned != 0:
                    continue
                values[f"{content_type}_actual"] = 0.0
                values[f"{content_type}_views"] = 0.0
