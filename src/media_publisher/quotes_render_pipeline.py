from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.google_sheets import GoogleSheetsClient
from media_publisher.sources.quote_layouts import QuoteLayoutConfig, load_quote_layout_config
from media_publisher.sources.quote_renderer import (
    QuoteRenderError,
    QuoteRenderPlan,
    render_quote_image,
    save_quote_image,
    select_render_plan,
)
from media_publisher.sources.quotes import LocalQuotePost, _publish_datetime
from media_publisher.sources.quotes_config import QuotesSourcesConfig
from media_publisher.sources.quotes_sheet import DailyQuoteText, load_monthly_quote_texts


class QuotesRenderPipelineError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedQuoteImage:
    variant: str
    day: int
    stem: str
    image_path: Path
    caption: str
    layout_key: str
    line_count: int
    background_name: str
    caption_source: str = "ready"


def _background_cache_path(
    config: QuotesSourcesConfig,
    *,
    variant: str,
    background_name: str,
) -> Path:
    return config.variant_background_dir(variant) / background_name


def _render_output_path(
    config: QuotesSourcesConfig,
    *,
    variant: str,
    year: int,
    month: int,
    day: int,
) -> Path:
    stem = f"{year:04d}-{month:02d}-{day:02d}"
    return config.variant_render_dir(variant) / f"{stem}.jpg"


def render_monthly_quotes(
    *,
    config: QuotesSourcesConfig,
    sheets_client: GoogleSheetsClient,
    drive_client: GoogleDriveClient,
    year: int,
    month: int,
    variants: tuple[str, ...] = ("fbyt", "ig"),
    font_path: Path | None = None,
    overwrite: bool = False,
    day: int | None = None,
    require_ready: bool = True,
) -> list[RenderedQuoteImage]:
    from media_publisher.quotes_text_sync import resolve_bulgarian_spreadsheet_id

    quotes = load_monthly_quote_texts(
        sheets_client,
        config,
        year=year,
        month=month,
        require_ready=require_ready,
        spreadsheet_id=resolve_bulgarian_spreadsheet_id(
            drive=drive_client, config=config, year=year
        ),
    )
    if day is not None:
        quotes = [quote for quote in quotes if quote.day == day]
    if not quotes:
        scope = (
            f"{year}-{month:02d}-{day:02d}"
            if day is not None
            else f"{year}-{month:02d}"
        )
        raise QuotesRenderPipelineError(
            f"No quote rows found for {scope} in the configured spreadsheet."
        )

    drive_config = config.backgrounds_drive
    month_folder = drive_client.resolve_month_background_folder(
        root_folder_id=str(drive_config["root_folder_id"]),
        year=year,
        month=month,
        year_folder_pattern=str(drive_config["year_folder_pattern"]),
        month_folder_pattern=str(drive_config["month_folder_pattern"]),
    )

    rendered: list[RenderedQuoteImage] = []
    for variant in variants:
        variant_drive = drive_config.get("variants", {}).get(variant, {})
        if not isinstance(variant_drive, dict):
            raise QuotesRenderPipelineError(f"Unknown variant {variant!r}")

        subdir = variant_drive.get("subdir")
        backgrounds = drive_client.list_quote_backgrounds(
            month_folder_id=month_folder.id,
            variant=variant,
            subdir=subdir if isinstance(subdir, str) and subdir.strip() else None,
            month=month,
        )
        background_by_day = {item.day: item for item in backgrounds}

        layout_config = load_quote_layout_config(
            config.variant_layouts_config(variant),
            template_dir=config.variant_template_dir(variant),
        )

        for quote in quotes:
            background = background_by_day.get(quote.day)
            if background is None:
                raise QuotesRenderPipelineError(
                    f"No {variant} background found for day {quote.day} in "
                    f"{month_folder.name!r}"
                )

            destination = _render_output_path(
                config,
                variant=variant,
                year=year,
                month=month,
                day=quote.day,
            )
            if destination.is_file() and not overwrite:
                rendered.append(
                    RenderedQuoteImage(
                        variant=variant,
                        day=quote.day,
                        stem=destination.stem,
                        image_path=destination,
                        caption=quote.text_bg,
                        layout_key="cached",
                        line_count=0,
                        background_name=background.name,
                        caption_source=quote.text_source,
                    )
                )
                continue

            cache_path = _background_cache_path(
                config,
                variant=variant,
                background_name=background.name,
            )
            if not cache_path.is_file():
                drive_client.download_file(background.file_id, cache_path)

            plan = select_render_plan(layout_config, quote.text_bg, font_path=font_path)
            image = render_quote_image(
                background_path=cache_path,
                layout_config=layout_config,
                plan=plan,
                font_path=font_path,
            )
            save_quote_image(image, destination)
            rendered.append(
                RenderedQuoteImage(
                    variant=variant,
                    day=quote.day,
                    stem=destination.stem,
                    image_path=destination,
                    caption=quote.text_bg,
                    layout_key=plan.layout_key,
                    line_count=len(plan.lines),
                    background_name=background.name,
                    caption_source=quote.text_source,
                )
            )

    return rendered


def resolve_quote_days_to_prepare(
    *,
    year: int,
    month: int,
    publish_mode: str,
    reference_date: date | None,
    platforms: tuple[str, ...] | None = None,
) -> set[int]:
    """Return calendar days that need rendered images for this publish run.

    In staggered mode, Instagram uses today's quote and YouTube/Facebook use
    tomorrow's. When ``platforms`` is set, only days needed for those platforms
    are prepared (so an Instagram-only run does not require tomorrow's row).
    """
    days_in_month = calendar.monthrange(year, month)[1]
    all_days = set(range(1, days_in_month + 1))

    if publish_mode == "staggered":
        if reference_date is None:
            raise QuotesRenderPipelineError(
                "reference_date is required for staggered quote publish"
            )
        need_today = platforms is None or "instagram" in platforms
        need_tomorrow = platforms is None or any(
            platform in platforms for platform in ("youtube", "facebook")
        )
        days: set[int] = set()
        if need_today:
            days.add(reference_date.day)
        if need_tomorrow:
            tomorrow = reference_date + timedelta(days=1)
            if tomorrow.year == year and tomorrow.month == month:
                days.add(tomorrow.day)
        return days & all_days

    if reference_date is not None:
        if reference_date.year != year or reference_date.month != month:
            return set()
        return {reference_date.day}

    return all_days


def build_local_quote_posts(
    fbyt_renders: list[RenderedQuoteImage],
    *,
    publish_timezone: str,
    publish_hour: int,
) -> list[LocalQuotePost]:
    posts: list[LocalQuotePost] = []
    for item in sorted(fbyt_renders, key=lambda render: render.day):
        year, month, day = (int(part) for part in item.stem.split("-"))
        posts.append(
            LocalQuotePost(
                source_path=item.image_path,
                image_path=item.image_path,
                publish_at=_publish_datetime(
                    year,
                    month,
                    day,
                    publish_timezone=publish_timezone,
                    publish_hour=publish_hour,
                ),
                caption=item.caption,
                stem=item.stem,
                caption_source=item.caption_source,
            )
        )
    return posts


def prepare_quote_posts_for_publish(
    *,
    config: QuotesSourcesConfig,
    sheets_client: GoogleSheetsClient,
    drive_client: GoogleDriveClient,
    year: int,
    month: int,
    publish_timezone: str,
    publish_hour: int,
    publish_mode: str,
    reference_date: date | None,
    platforms: tuple[str, ...] | None = None,
    overwrite: bool = False,
) -> tuple[list[LocalQuotePost], dict[str, Path]]:
    """Render quote images from Sheet text + Drive backgrounds and build publish posts."""
    days = resolve_quote_days_to_prepare(
        year=year,
        month=month,
        publish_mode=publish_mode,
        reference_date=reference_date,
        platforms=platforms,
    )
    if not days:
        return [], {}

    from media_publisher.quotes_text_sync import resolve_bulgarian_spreadsheet_id

    available_days = {
        quote.day
        for quote in load_monthly_quote_texts(
            sheets_client,
            config,
            year=year,
            month=month,
            spreadsheet_id=resolve_bulgarian_spreadsheet_id(
                drive=drive_client, config=config, year=year
            ),
            require_ready=False,
        )
    }
    requested_days = sorted(days)
    days = days & available_days
    if not days:
        requested = ", ".join(
            f"{year}-{month:02d}-{day:02d}" for day in requested_days
        )
        raise QuotesRenderPipelineError(
            f"No quote rows found for {requested} in the configured spreadsheet."
        )

    need_instagram = platforms is None or "instagram" in platforms
    need_fbyt = platforms is None or any(
        platform in platforms for platform in ("youtube", "facebook")
    )
    # Posts are built from FB/YT renders; Instagram still needs a post shell even
    # when YouTube/Facebook are not publishing in this run.
    if need_instagram:
        need_fbyt = True

    fbyt_renders: list[RenderedQuoteImage] = []
    ig_renders: list[RenderedQuoteImage] = []
    for day in sorted(days):
        if need_fbyt:
            fbyt_renders.extend(
                render_monthly_quotes(
                    config=config,
                    sheets_client=sheets_client,
                    drive_client=drive_client,
                    year=year,
                    month=month,
                    variants=("fbyt",),
                    overwrite=overwrite,
                    day=day,
                    require_ready=False,
                )
            )
        if need_instagram:
            ig_renders.extend(
                render_monthly_quotes(
                    config=config,
                    sheets_client=sheets_client,
                    drive_client=drive_client,
                    year=year,
                    month=month,
                    variants=("ig",),
                    overwrite=overwrite,
                    day=day,
                    require_ready=False,
                )
            )

    posts = build_local_quote_posts(
        fbyt_renders,
        publish_timezone=publish_timezone,
        publish_hour=publish_hour,
    )
    ig_images_by_stem = {item.stem: item.image_path for item in ig_renders}
    return posts, ig_images_by_stem
