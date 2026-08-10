from __future__ import annotations

from datetime import date
from typing import Any

from media_publisher.analytics.meta_analytics import month_key, months_between
from media_publisher.sources.airtable import (
    FIELD_SG_FB_DATE,
    FIELD_SG_FB_PUBLISHED,
    FIELD_SG_IG_DATE,
    FIELD_SG_IG_PUBLISHED,
    FIELD_SG_YT_DATE,
    FIELD_SG_YT_PUBLISHED,
    FIELD_TYPE,
    PLATFORM_FIELD_CONFIGS,
    TYPE_QUOTE,
    TYPE_REEL,
    TYPE_SHORT,
    TYPE_VIDEO,
    AirtableClient,
    AirtableError,
    _field_text,
    _parse_publish_at,
)

SHORT_FORM_TYPES = {TYPE_SHORT, TYPE_REEL, TYPE_QUOTE}


def _empty_platform_months(
    start_month: date,
    end_month: date,
) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {
        "youtube": {},
        "facebook": {},
        "instagram": {},
    }
    count = months_between(start_month, end_month) + 1
    current = date(start_month.year, start_month.month, 1)
    for _ in range(max(count, 0)):
        key = month_key(current.year, current.month)
        for platform in result:
            result[platform][key] = {
                "lau_actual": 0.0,
                "shorts_actual": 0.0,
            }
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return result


def _published_present(value: Any) -> bool:
    return bool(_field_text(value))


def fetch_airtable_monthly_actual_counts(
    client: AirtableClient,
    *,
    start_month: date,
    end_month: date,
    timezone: str = "Europe/Sofia",
) -> dict[str, dict[str, dict[str, float]]]:
    """Count published Airtable rows per platform/month as LAU vs Shorts Actual.

    A row counts for a platform when:
    - SG-*-Date published falls in the month (Europe/Sofia calendar day)
    - SG-*-Published video is non-empty
    - Type Video → lau_actual; Short/Reel/Quote → shorts_actual
    """
    result = _empty_platform_months(start_month, end_month)
    end_exclusive = date(end_month.year + (1 if end_month.month == 12 else 0), (end_month.month % 12) + 1, 1)

    fields = [
        FIELD_TYPE,
        FIELD_SG_YT_DATE,
        FIELD_SG_FB_DATE,
        FIELD_SG_IG_DATE,
        FIELD_SG_YT_PUBLISHED,
        FIELD_SG_FB_PUBLISHED,
        FIELD_SG_IG_PUBLISHED,
    ]

    filter_formula = (
        "OR("
        f"AND({{{FIELD_SG_YT_DATE}}}, "
        f"NOT(IS_BEFORE({{{FIELD_SG_YT_DATE}}}, '{start_month.isoformat()}')), "
        f"IS_BEFORE({{{FIELD_SG_YT_DATE}}}, '{end_exclusive.isoformat()}')), "
        f"AND({{{FIELD_SG_FB_DATE}}}, "
        f"NOT(IS_BEFORE({{{FIELD_SG_FB_DATE}}}, '{start_month.isoformat()}')), "
        f"IS_BEFORE({{{FIELD_SG_FB_DATE}}}, '{end_exclusive.isoformat()}')), "
        f"AND({{{FIELD_SG_IG_DATE}}}, "
        f"NOT(IS_BEFORE({{{FIELD_SG_IG_DATE}}}, '{start_month.isoformat()}')), "
        f"IS_BEFORE({{{FIELD_SG_IG_DATE}}}, '{end_exclusive.isoformat()}'))"
        ")"
    )

    try:
        records = client.list_records(fields=fields, filter_formula=filter_formula)
    except AirtableError:
        # Fall back if formula quirks fail (still month-filtered locally).
        records = client.list_records(fields=fields)

    for record in records:
        type_text = _field_text(record.fields.get(FIELD_TYPE))
        if type_text == TYPE_VIDEO:
            bucket = "lau_actual"
        elif type_text in SHORT_FORM_TYPES:
            bucket = "shorts_actual"
        else:
            continue

        for config in PLATFORM_FIELD_CONFIGS:
            if not _published_present(record.fields.get(config.published_field)):
                continue
            published_at = _parse_publish_at(
                record.fields.get(config.date_field),
                publish_timezone=timezone,
            )
            if published_at is None:
                continue
            key = month_key(published_at.year, published_at.month)
            platform_months = result.get(config.platform, {})
            if key not in platform_months:
                continue
            platform_months[key][bucket] = platform_months[key].get(bucket, 0.0) + 1.0

    return result


def merge_actual_counts(
    target: dict[str, dict[str, dict[str, float]]],
    source: dict[str, dict[str, dict[str, float]]],
) -> None:
    for platform, months in source.items():
        platform_target = target.setdefault(platform, {})
        for month, values in months.items():
            entry = platform_target.setdefault(month, {})
            for key, value in values.items():
                if key in ("lau_actual", "shorts_actual"):
                    entry[key] = float(value)
