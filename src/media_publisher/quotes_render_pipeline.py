from __future__ import annotations

from dataclasses import dataclass
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
) -> list[RenderedQuoteImage]:
    quotes = load_monthly_quote_texts(
        sheets_client,
        config,
        year=year,
        month=month,
    )
    if day is not None:
        quotes = [quote for quote in quotes if quote.day == day]
    if not quotes:
        raise QuotesRenderPipelineError(
            f"No quote rows found for {year}-{month:02d} in the configured spreadsheet."
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
                )
            )

    return rendered
