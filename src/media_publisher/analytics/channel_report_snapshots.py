from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from media_publisher.analytics.meta_analytics import MetaAnalyticsError
from media_publisher.analytics.youtube_analytics import YouTubeAnalyticsError
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.publishers.youtube import YouTubeClient, YouTubePublishError


DEFAULT_SNAPSHOT_PATH = "data/channel_report_snapshots.json"
SHORT_RETENTION_METRICS = frozenset({"followers"})


class ChannelReportSnapshotError(RuntimeError):
    pass


@dataclass
class SnapshotStore:
    version: int = 1
    points: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotCaptureResult:
    captured_on: date
    recorded: list[tuple[str, str, float]]


def load_snapshot_store(path: Path) -> SnapshotStore:
    if not path.is_file():
        return SnapshotStore()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ChannelReportSnapshotError(f"Snapshot file is invalid: {path}")
    points = payload.get("points", {})
    if not isinstance(points, dict):
        raise ChannelReportSnapshotError(f"Snapshot file is invalid: {path}")
    return SnapshotStore(version=int(payload.get("version", 1)), points=_normalize_points(points))


def save_snapshot_store(path: Path, store: SnapshotStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": store.version,
        "points": store.points,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def record_snapshot(
    store: SnapshotStore,
    *,
    platform: str,
    metric_key: str,
    value: float,
    captured_on: date,
) -> None:
    key = captured_on.isoformat()
    store.points.setdefault(platform, {}).setdefault(metric_key, {})[key] = value


def latest_value_for_month(
    store: SnapshotStore,
    *,
    platform: str,
    metric_key: str,
    year: int,
    month: int,
) -> float | None:
    platform_points = store.points.get(platform, {}).get(metric_key, {})
    if not platform_points:
        return None
    prefix = f"{year:04d}-{month:02d}-"
    matches = [
        (day, value)
        for day, value in platform_points.items()
        if day.startswith(prefix)
    ]
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[-1][1]


def apply_snapshots_to_monthly_metrics(
    metrics: dict[str, dict[str, dict[str, float]]],
    store: SnapshotStore | None,
    *,
    start_month: date,
    end_month: date,
) -> None:
    if store is None:
        return

    cursor = start_month
    while cursor <= end_month:
        for platform in ("youtube", "facebook", "instagram"):
            for metric_key in SHORT_RETENTION_METRICS:
                snapshot_value = latest_value_for_month(
                    store,
                    platform=platform,
                    metric_key=metric_key,
                    year=cursor.year,
                    month=cursor.month,
                )
                if snapshot_value is None:
                    continue
                month_key = f"{cursor.year:04d}-{cursor.month:02d}"
                metrics[platform].setdefault(month_key, {})[metric_key] = snapshot_value
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)


def capture_channel_report_snapshots(
    *,
    store_path: Path,
    meta_client: MetaClient | None,
    meta_page_id: str | None,
    meta_instagram_account_id: str | None,
    youtube_client: YouTubeClient | None,
    youtube_channel_id: str | None,
    captured_on: date | None = None,
) -> SnapshotCaptureResult:
    captured = captured_on or date.today()
    store = load_snapshot_store(store_path)
    recorded: list[tuple[str, str, float]] = []

    if meta_client is not None and meta_page_id:
        try:
            followers = _fetch_facebook_followers_count(meta_client, meta_page_id)
        except MetaAnalyticsError as exc:
            raise ChannelReportSnapshotError(str(exc)) from exc
        if followers is not None:
            record_snapshot(
                store,
                platform="facebook",
                metric_key="followers",
                value=float(followers),
                captured_on=captured,
            )
            recorded.append(("facebook", "followers", float(followers)))

    if meta_client is not None and meta_instagram_account_id:
        try:
            followers = _fetch_instagram_followers_count(
                meta_client,
                meta_instagram_account_id,
            )
        except MetaAnalyticsError as exc:
            raise ChannelReportSnapshotError(str(exc)) from exc
        if followers is not None:
            record_snapshot(
                store,
                platform="instagram",
                metric_key="followers",
                value=float(followers),
                captured_on=captured,
            )
            recorded.append(("instagram", "followers", float(followers)))

    if youtube_client is not None and youtube_channel_id:
        try:
            subscribers = _fetch_youtube_subscriber_count(
                youtube_client,
                youtube_channel_id,
            )
        except YouTubeAnalyticsError as exc:
            raise ChannelReportSnapshotError(str(exc)) from exc
        if subscribers is not None:
            record_snapshot(
                store,
                platform="youtube",
                metric_key="followers",
                value=float(subscribers),
                captured_on=captured,
            )
            recorded.append(("youtube", "followers", float(subscribers)))

    save_snapshot_store(store_path, store)
    return SnapshotCaptureResult(captured_on=captured, recorded=recorded)


def _fetch_facebook_followers_count(client: MetaClient, page_id: str) -> int | None:
    try:
        response = client._request(
            "GET",
            page_id,
            query={"fields": "followers_count,fan_count"},
        )
    except MetaError as exc:
        raise MetaAnalyticsError(str(exc)) from exc
    if not isinstance(response, dict):
        return None
    for key in ("followers_count", "fan_count"):
        value = response.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _fetch_instagram_followers_count(
    client: MetaClient,
    instagram_account_id: str,
) -> int | None:
    try:
        response = client._request(
            "GET",
            instagram_account_id,
            query={"fields": "followers_count"},
        )
    except MetaError as exc:
        raise MetaAnalyticsError(str(exc)) from exc
    if not isinstance(response, dict):
        return None
    value = response.get("followers_count")
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _fetch_youtube_subscriber_count(
    client: YouTubeClient,
    channel_id: str,
) -> int | None:
    import urllib.parse

    from media_publisher.publishers.youtube import API_BASE

    try:
        access_token = client.ensure_access_token()
    except YouTubePublishError as exc:
        raise YouTubeAnalyticsError(str(exc)) from exc

    query = urllib.parse.urlencode(
        {
            "part": "statistics",
            "id": channel_id,
        }
    )
    import urllib.request

    request = urllib.request.Request(f"{API_BASE}/channels?{query}")
    request.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise YouTubeAnalyticsError(f"YouTube subscriber lookup failed: {exc}") from exc

    if not isinstance(payload, dict):
        return None
    items = payload.get("items", [])
    if not isinstance(items, list) or not items:
        return None
    statistics = items[0].get("statistics", {})
    if not isinstance(statistics, dict):
        return None
    value = statistics.get("subscriberCount")
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _normalize_points(
    points: dict[str, Any],
) -> dict[str, dict[str, dict[str, float]]]:
    normalized: dict[str, dict[str, dict[str, float]]] = {}
    for platform, metrics in points.items():
        if not isinstance(metrics, dict):
            continue
        platform_metrics: dict[str, dict[str, float]] = {}
        for metric_key, values in metrics.items():
            if not isinstance(values, dict):
                continue
            metric_values: dict[str, float] = {}
            for day, value in values.items():
                if isinstance(day, str) and isinstance(value, (int, float)):
                    metric_values[day] = float(value)
            if metric_values:
                platform_metrics[str(metric_key)] = metric_values
        if platform_metrics:
            normalized[str(platform)] = platform_metrics
    return normalized
