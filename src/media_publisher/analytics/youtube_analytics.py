from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any

from media_publisher.publishers.youtube import YouTubePublishError


YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2/reports"


@dataclass(frozen=True)
class MonthlyViews:
    year: int
    month: int
    views: int


class YouTubeAnalyticsError(RuntimeError):
    pass


def fetch_youtube_monthly_views(
    *,
    access_token: str,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> list[MonthlyViews]:
    """Return channel views aggregated by calendar month."""
    metrics = fetch_youtube_monthly_metrics(
        access_token=access_token,
        channel_id=channel_id,
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


def fetch_youtube_monthly_metrics(
    *,
    access_token: str,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, float]]:
    if start_month.day != 1 or end_month.day != 1:
        raise YouTubeAnalyticsError("start_month and end_month must be the first day of a month")
    if end_month < start_month:
        raise YouTubeAnalyticsError("end_month must be on or after start_month")

    channel_metrics = _fetch_youtube_monthly_metric_rows(
        access_token=access_token,
        channel_id=channel_id,
        start_month=start_month,
        end_month=end_month,
        metric_names=("views", "estimatedMinutesWatched"),
    )
    content_type_metrics = _fetch_youtube_monthly_content_type_views(
        access_token=access_token,
        channel_id=channel_id,
        start_month=start_month,
        end_month=end_month,
    )
    merged: dict[str, dict[str, float]] = {}
    for key, values in channel_metrics.items():
        total_views = values.get("views", 0.0)
        content_types = content_type_metrics.get(key, {})
        shorts_views = content_types.get("SHORTS", 0.0)
        video_views = content_types.get("VIDEO_ON_DEMAND", 0.0)
        if video_views <= 0.0 and shorts_views > 0.0:
            video_views = max(total_views - shorts_views, 0.0)
        if shorts_views <= 0.0 and video_views > 0.0:
            shorts_views = max(total_views - video_views, 0.0)
        if video_views <= 0.0 and shorts_views <= 0.0:
            video_views = total_views
        merged[key] = dict(values)
        merged[key]["video_views"] = video_views
        merged[key]["total_views"] = total_views
        merged[key]["watch_time_hours"] = values.get("estimatedMinutesWatched", 0.0) / 60.0
        merged[key]["shorts_views"] = shorts_views
        merged[key]["lau_views"] = video_views
    return merged


def _fetch_youtube_monthly_content_type_views(
    *,
    access_token: str,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, float]]:
    query_params = {
        "ids": f"channel=={channel_id}",
        "startDate": start_month.isoformat(),
        "endDate": _analytics_end_date(end_month),
        "metrics": "views",
        "dimensions": "month,creatorContentType",
        "sort": "month",
    }
    query = urllib.parse.urlencode(query_params)
    url = f"{YOUTUBE_ANALYTICS_BASE}?{query}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {access_token}")

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        return {}
    except urllib.error.URLError:
        return {}

    if not isinstance(payload, dict):
        return {}

    headers = payload.get("columnHeaders", [])
    rows = payload.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list):
        return {}

    month_index = _column_index(headers, "month")
    content_type_index = _column_index(headers, "creatorContentType")
    views_index = _column_index(headers, "views")
    if month_index is None or content_type_index is None or views_index is None:
        return {}

    monthly: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, list):
            continue
        if len(row) <= max(month_index, content_type_index, views_index):
            continue
        parsed_month = _parse_month_value(row[month_index])
        content_type = row[content_type_index]
        if parsed_month is None or not isinstance(content_type, str):
            continue
        key = f"{parsed_month[0]:04d}-{parsed_month[1]:02d}"
        monthly.setdefault(key, {})
        monthly[key][content_type] = _parse_number(row[views_index])
    return monthly


def fetch_youtube_monthly_metrics_for_client(
    client: Any,
    *,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, float]]:
    try:
        access_token = client.ensure_access_token()
    except YouTubePublishError as exc:
        raise YouTubeAnalyticsError(str(exc)) from exc
    return fetch_youtube_monthly_metrics(
        access_token=access_token,
        channel_id=channel_id,
        start_month=start_month,
        end_month=end_month,
    )


def _fetch_youtube_monthly_metric_rows(
    *,
    access_token: str,
    channel_id: str,
    start_month: date,
    end_month: date,
    metric_names: tuple[str, ...],
    filters: str | None = None,
) -> dict[str, dict[str, float]]:
    query_params = {
        "ids": f"channel=={channel_id}",
        "startDate": start_month.isoformat(),
        "endDate": _analytics_end_date(end_month),
        "metrics": ",".join(metric_names),
        "dimensions": "month",
        "sort": "month",
    }
    if filters:
        query_params["filters"] = filters
    query = urllib.parse.urlencode(query_params)
    url = f"{YOUTUBE_ANALYTICS_BASE}?{query}"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {access_token}")

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise YouTubeAnalyticsError(
            f"YouTube Analytics request failed with HTTP {exc.code}: {detail}"
        ) from exc
    except urllib.error.URLError as exc:
        raise YouTubeAnalyticsError(
            f"YouTube Analytics request failed: {exc.reason}"
        ) from exc

    if not isinstance(payload, dict):
        raise YouTubeAnalyticsError("YouTube Analytics response is invalid")

    headers = payload.get("columnHeaders", [])
    rows = payload.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list):
        return {}

    month_index = _column_index(headers, "month")
    metric_indices = {
        name: _column_index(headers, name)
        for name in metric_names
        if _column_index(headers, name) is not None
    }
    if month_index is None:
        raise YouTubeAnalyticsError("YouTube Analytics response is missing month column")

    monthly: dict[str, dict[str, float]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) <= month_index:
            continue
        parsed_month = _parse_month_value(row[month_index])
        if parsed_month is None:
            continue
        key = f"{parsed_month[0]:04d}-{parsed_month[1]:02d}"
        values: dict[str, float] = {}
        for name, index in metric_indices.items():
            if index is None or len(row) <= index:
                continue
            values[name] = _parse_number(row[index])
        monthly[key] = values
    return monthly


def _analytics_end_date(end_month: date) -> str:
    if end_month.month == 12:
        return date(end_month.year + 1, 1, 1).isoformat()
    return date(end_month.year, end_month.month + 1, 1).isoformat()


def _parse_number(value: Any) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return 0.0
        try:
            return float(text)
        except ValueError:
            return 0.0
    return 0.0


def fetch_youtube_monthly_views_for_client(
    client: Any,
    *,
    channel_id: str,
    start_month: date,
    end_month: date,
) -> list[MonthlyViews]:
    try:
        access_token = client.ensure_access_token()
    except YouTubePublishError as exc:
        raise YouTubeAnalyticsError(str(exc)) from exc
    return fetch_youtube_monthly_views(
        access_token=access_token,
        channel_id=channel_id,
        start_month=start_month,
        end_month=end_month,
    )


def _column_index(headers: list[Any], name: str) -> int | None:
    for index, header in enumerate(headers):
        if isinstance(header, dict) and header.get("name") == name:
            return index
    return None


def _parse_month_value(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 7 or text[4] != "-":
        return None
    try:
        year = int(text[:4])
        month = int(text[5:7])
    except ValueError:
        return None
    if month < 1 or month > 12:
        return None
    return year, month


def _parse_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0
