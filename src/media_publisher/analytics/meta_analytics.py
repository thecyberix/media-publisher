from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from media_publisher.publishers.meta import MetaClient, MetaError


@dataclass(frozen=True)
class MonthlyViews:
    year: int
    month: int
    views: int


class MetaAnalyticsError(RuntimeError):
    pass


# Meta insights reject ranges where until - since is greater than 30 days (2_592_000 s).
# Use 29 days per request to stay safely under the limit.
MAX_INSIGHT_WINDOW_SECONDS = 29 * 24 * 60 * 60


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _month_range(start_month: date, end_month: date) -> list[tuple[int, int]]:
    if start_month.day != 1 or end_month.day != 1:
        raise MetaAnalyticsError("start_month and end_month must be the first day of a month")
    if end_month < start_month:
        raise MetaAnalyticsError("end_month must be on or after start_month")

    months: list[tuple[int, int]] = []
    cursor = start_month
    while cursor <= end_month:
        months.append((cursor.year, cursor.month))
        cursor = _next_month(cursor)
    return months


def _sum_insight_values(payload: dict[str, Any]) -> int:
    data = payload.get("data", [])
    if not isinstance(data, list):
        return 0
    total = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        total_value = item.get("total_value")
        if isinstance(total_value, dict):
            value = total_value.get("value")
            if isinstance(value, (int, float)):
                total += int(value)
                continue
        values = item.get("values", [])
        if not isinstance(values, list):
            continue
        for entry in values:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if isinstance(value, (int, float)):
                total += int(value)
    return total


def _insight_windows_for_month(year: int, month: int) -> list[tuple[int, int]]:
    """Split a calendar month into Meta-compatible insight time windows."""
    month_start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = _next_month(_month_start(year, month))
    month_end = datetime(next_month.year, next_month.month, 1, tzinfo=timezone.utc)
    since_ts = int(month_start.timestamp())
    end_ts = int(month_end.timestamp())

    windows: list[tuple[int, int]] = []
    cursor = since_ts
    while cursor < end_ts:
        until = min(cursor + MAX_INSIGHT_WINDOW_SECONDS, end_ts)
        windows.append((cursor, until))
        cursor = until
    return windows


def _fetch_monthly_insight_total(
    *,
    year: int,
    month: int,
    fetch_window: Any,
) -> int:
    total = 0
    for since, until in _insight_windows_for_month(year, month):
        try:
            payload = fetch_window(since=since, until=until)
        except MetaError as exc:
            raise MetaAnalyticsError(str(exc)) from exc
        total += _sum_insight_values(payload)
    return total


def _try_fetch_monthly_metric(
    *,
    year: int,
    month: int,
    fetch_window: Any,
) -> float | None:
    try:
        return float(
            _fetch_monthly_insight_total(
                year=year,
                month=month,
                fetch_window=fetch_window,
            )
        )
    except MetaAnalyticsError:
        return None


def _try_fetch_month_end_snapshot(
    *,
    year: int,
    month: int,
    fetch_window: Any,
) -> float | None:
    try:
        return float(
            _fetch_month_end_snapshot(
                year=year,
                month=month,
                fetch_window=fetch_window,
            )
        )
    except MetaAnalyticsError:
        return None


def _set_metric(
    values: dict[str, float],
    key: str,
    metric_value: float | None,
) -> None:
    if metric_value is not None:
        values[key] = metric_value


def fetch_facebook_monthly_video_views(
    client: MetaClient,
    *,
    page_id: str,
    start_month: date,
    end_month: date,
) -> list[MonthlyViews]:
    metrics = fetch_facebook_monthly_metrics(
        client,
        page_id=page_id,
        start_month=start_month,
        end_month=end_month,
    )
    monthly: list[MonthlyViews] = []
    for key, values in sorted(metrics.items()):
        year = int(key[:4])
        month = int(key[5:7])
        monthly.append(
            MonthlyViews(
                year=year,
                month=month,
                views=int(values.get("video_views", 0)),
            )
        )
    return monthly


def fetch_facebook_monthly_metrics(
    client: MetaClient,
    *,
    page_id: str,
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, float]]:
    monthly: dict[str, dict[str, float]] = {}
    for year, month in _month_range(start_month, end_month):
        key = month_key(year, month)
        values: dict[str, float] = {}
        _set_metric(
            values,
            "video_views",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_page_insights(
                    page_id,
                    metric="page_video_views",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "reach",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_page_insights(
                    page_id,
                    metric="page_impressions_unique",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "total_views",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_page_insights(
                    page_id,
                    metric="page_impressions",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "followers",
            _try_fetch_month_end_snapshot(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_page_insights(
                    page_id,
                    metric="page_fans",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        if "video_views" in values:
            values.setdefault("shorts_views", values["video_views"])
            values.setdefault("lau_views", values["video_views"])
        monthly[key] = values
    return monthly


def fetch_instagram_monthly_views(
    client: MetaClient,
    *,
    instagram_account_id: str,
    start_month: date,
    end_month: date,
) -> list[MonthlyViews]:
    metrics = fetch_instagram_monthly_metrics(
        client,
        instagram_account_id=instagram_account_id,
        start_month=start_month,
        end_month=end_month,
    )
    monthly: list[MonthlyViews] = []
    for key, values in sorted(metrics.items()):
        year = int(key[:4])
        month = int(key[5:7])
        monthly.append(
            MonthlyViews(
                year=year,
                month=month,
                views=int(values.get("video_views", 0)),
            )
        )
    return monthly


def fetch_instagram_monthly_metrics(
    client: MetaClient,
    *,
    instagram_account_id: str,
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, float]]:
    monthly: dict[str, dict[str, float]] = {}
    for year, month in _month_range(start_month, end_month):
        key = month_key(year, month)
        values: dict[str, float] = {}
        _set_metric(
            values,
            "video_views",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_instagram_account_insights(
                    instagram_account_id,
                    metric="views",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "reach",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_instagram_account_insights(
                    instagram_account_id,
                    metric="reach",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "total_views",
            _try_fetch_monthly_metric(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_instagram_account_insights(
                    instagram_account_id,
                    metric="views",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        _set_metric(
            values,
            "followers",
            _try_fetch_month_end_snapshot(
                year=year,
                month=month,
                fetch_window=lambda since, until: client.get_instagram_account_insights(
                    instagram_account_id,
                    metric="follower_count",
                    since=since,
                    until=until,
                    period="day",
                ),
            ),
        )
        if "video_views" in values:
            values.setdefault("shorts_views", values["video_views"])
            values.setdefault("lau_views", values["video_views"])
        monthly[key] = values
    return monthly


def _fetch_month_end_snapshot(
    *,
    year: int,
    month: int,
    fetch_window: Any,
) -> int:
    values: list[int] = []
    for since, until in _insight_windows_for_month(year, month):
        try:
            payload = fetch_window(since=since, until=until)
        except MetaError as exc:
            raise MetaAnalyticsError(str(exc)) from exc
        data = payload.get("data", [])
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            entries = item.get("values", [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("value")
                if isinstance(value, (int, float)):
                    values.append(int(value))
    if not values:
        return 0
    return values[-1]



def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def last_complete_month(*, today: date | None = None) -> date:
    current = today or date.today()
    year = current.year
    month = current.month - 1
    if month == 0:
        year -= 1
        month = 12
    return date(year, month, 1)


def months_between(start_month: date, end_month: date) -> int:
    return (end_month.year - start_month.year) * 12 + (end_month.month - start_month.month)
