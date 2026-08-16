from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time

from media_publisher.languages import get_language

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_event_date(value: str) -> date:
    text = value.strip()
    match = _DATE_RE.fullmatch(text)
    if not match:
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    year, month, day = (int(part) for part in match.groups())
    return date(year, month, day)


def parse_event_time(value: str) -> time:
    text = value.strip()
    match = _TIME_RE.fullmatch(text)
    if not match:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        raise ValueError(f"time must be HH:MM, got {value!r}")
    return time(hour, minute)


def format_event_datetime(
    event_date: date,
    event_time: time,
    *,
    language: str = "bg",
) -> str:
    clock = f"{event_time.hour:02d}:{event_time.minute:02d}"
    definition = get_language(language)
    if definition is not None:
        month_name = definition.month_name(event_date.month)
        return (
            f"{event_date.day} {month_name} {event_date.year}"
            f"{definition.date_year_suffix}, {clock}"
        )
    month_name = calendar.month_name[event_date.month]
    return f"{event_date.day} {month_name} {event_date.year}, {clock}"


def format_bulgarian_datetime(event_date: date, event_time: time) -> str:
    return format_event_datetime(event_date, event_time, language="bg")


def format_iso_local(event_date: date, event_time: time) -> str:
    return datetime.combine(event_date, event_time).isoformat(timespec="minutes")
