from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from media_publisher.languages import selected_language
from media_publisher.timezones import get_timezone
from media_publisher.sources.quote_pdf import (
    ensure_quote_image_from_pdf_page,
    extract_pdf_page_text,
)

DEFAULT_QUOTES_PUBLISH_TIMEZONE = "Europe/Sofia"
DEFAULT_QUOTES_PUBLISH_HOUR = 8
QUOTE_RENDER_DIRNAME = ".renders"
QUOTE_IG_RENDER_DIRNAME = ".renders-ig"
QUOTE_VIDEO_DIRNAME = ".videos"
STATE_FILENAME = ".schedule-state.json"
QUOTE_CANVA_DESIGN_TITLE_TEMPLATE = "{month_name} {year} FB/YT DMQ Template Final"
QUOTE_CANVA_IG_DESIGN_TITLE_TEMPLATE = "{month_name} {year} IG DMQ Template Final"

ISO_DATE_STEM_RE = re.compile(r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
DMY_DATE_STEM_RE = re.compile(
    r"^(?P<day>\d{1,2})[.\-/](?P<month>\d{1,2})[.\-/](?P<year>\d{4})$"
)
BULGARIAN_DATE_STEM_RE = re.compile(
    r"^(?P<day>\d{1,2})\s+(?P<month>[A-Za-zА-Яа-я]+)$",
    re.UNICODE,
)


class QuoteDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalQuotePost:
    """A daily quote extracted from a monthly Canva PDF."""

    source_path: Path
    image_path: Path
    publish_at: datetime
    caption: str
    stem: str
    caption_source: str = "ready"


def _publish_datetime(
    year: int,
    month: int,
    day: int,
    *,
    publish_timezone: str,
    publish_hour: int,
) -> datetime:
    return datetime(
        year,
        month,
        day,
        publish_hour,
        0,
        tzinfo=get_timezone(publish_timezone),
    )


def _infer_year(day: int, month: int, *, today: date) -> int:
    candidate = date(today.year, month, day)
    if candidate >= today:
        return today.year
    return today.year + 1


def month_display_name(month: int) -> str:
    return selected_language().month_name(month).capitalize()


bulgarian_month_display_name = month_display_name


def quote_canva_design_title(year: int, month: int) -> str:
    return QUOTE_CANVA_DESIGN_TITLE_TEMPLATE.format(
        month_name=month_display_name(month),
        year=year,
    )


def quote_canva_ig_design_title(year: int, month: int) -> str:
    return QUOTE_CANVA_IG_DESIGN_TITLE_TEMPLATE.format(
        month_name=month_display_name(month),
        year=year,
    )


def parse_quote_date_from_stem(
    stem: str,
    *,
    publish_timezone: str = DEFAULT_QUOTES_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_QUOTES_PUBLISH_HOUR,
    today: date | None = None,
) -> datetime | None:
    """Parse a publish date from a quote filename stem."""
    text = stem.strip()
    if not text:
        return None

    current = today or datetime.now(get_timezone(publish_timezone)).date()

    match = ISO_DATE_STEM_RE.match(text)
    if match:
        return _publish_datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )

    match = DMY_DATE_STEM_RE.match(text)
    if match:
        return _publish_datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )

    match = BULGARIAN_DATE_STEM_RE.match(text)
    if match:
        month_name = match.group("month").casefold()
        month = selected_language().month_number(month_name)
        if month is None:
            return None
        day = int(match.group("day"))
        year = _infer_year(day, month, today=current)
        return _publish_datetime(
            year,
            month,
            day,
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )

    return None


def quote_state_path(work_dir: Path) -> Path:
    return work_dir / STATE_FILENAME


def load_quote_state(work_dir: Path) -> dict[str, dict[str, object]]:
    path = quote_state_path(work_dir)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QuoteDiscoveryError(f"Invalid quote state file: {path}")
    return payload


def save_quote_state(work_dir: Path, state: dict[str, dict[str, object]]) -> None:
    path = quote_state_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def platform_permalink(state_entry: dict[str, object], platform: str) -> str | None:
    platforms = state_entry.get("platforms")
    if not isinstance(platforms, dict):
        return None
    platform_state = platforms.get(platform)
    if not isinstance(platform_state, dict):
        return None
    permalink = platform_state.get("permalink")
    if isinstance(permalink, str) and permalink.strip():
        return permalink.strip()
    return None


def mark_platform_scheduled_in_state(
    state: dict[str, dict[str, object]],
    *,
    image_name: str,
    platform: str,
    permalink: str,
    publish_at: datetime,
    source_pdf: str | None = None,
) -> None:
    entry = dict(state.get(image_name, {}))
    entry["publish_at"] = publish_at.isoformat()
    if source_pdf:
        entry["source_pdf"] = source_pdf
    platforms = dict(entry.get("platforms", {})) if isinstance(entry.get("platforms"), dict) else {}
    platforms[platform] = {"permalink": permalink}
    entry["platforms"] = platforms
    state[image_name] = entry


def discover_monthly_quotes(
    pdf_path: Path,
    *,
    year: int,
    month: int,
    work_dir: Path,
    publish_timezone: str = DEFAULT_QUOTES_PUBLISH_TIMEZONE,
    publish_hour: int = DEFAULT_QUOTES_PUBLISH_HOUR,
    render_dirname: str = QUOTE_RENDER_DIRNAME,
) -> list[LocalQuotePost]:
    """Expand a monthly quotes PDF into one post per day (page N = day N)."""
    source = pdf_path.resolve()
    if not source.is_file():
        raise QuoteDiscoveryError(f"Monthly quotes PDF not found: {source}")

    try:
        import fitz
    except ModuleNotFoundError as exc:
        raise QuoteDiscoveryError(
            "Monthly quote discovery requires pymupdf. Install it with:\n"
            "  pip install pymupdf"
        ) from exc

    document = fitz.open(source)
    page_count = document.page_count
    document.close()
    if page_count == 0:
        raise QuoteDiscoveryError(f"Monthly quotes PDF has no pages: {source.name}")

    days_in_month = calendar.monthrange(year, month)[1]
    render_dir = work_dir / render_dirname
    posts: list[LocalQuotePost] = []

    for day in range(1, min(days_in_month, page_count) + 1):
        page_index = day - 1
        stem = f"{year:04d}-{month:02d}-{day:02d}"
        posts.append(
            LocalQuotePost(
                source_path=source,
                image_path=ensure_quote_image_from_pdf_page(
                    source,
                    page_index,
                    render_dir,
                ),
                publish_at=_publish_datetime(
                    year,
                    month,
                    day,
                    publish_timezone=publish_timezone,
                    publish_hour=publish_hour,
                ),
                caption=extract_pdf_page_text(source, page_index),
                stem=stem,
            )
        )

    return posts
