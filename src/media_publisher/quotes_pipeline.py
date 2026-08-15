from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from media_publisher.models import PlatformName
from media_publisher.pipeline import PlatformPublishResult
from media_publisher.publishers.facebook import FacebookPublishError
from media_publisher.publishers.instagram import InstagramPublishError
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.publishers.quotes import publish_local_quote
from media_publisher.publishers.youtube import (
    YouTubePublishError,
    flush_configured_daily_playlist,
)
from media_publisher.quotes_render_pipeline import (
    QuotesRenderPipelineError,
    prepare_quote_posts_for_publish,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.google_sheets import GoogleSheetsClient
from media_publisher.sources.quotes import (
    LocalQuotePost,
    QUOTE_VIDEO_DIRNAME,
    load_quote_state,
    mark_platform_scheduled_in_state,
    platform_permalink,
    save_quote_state,
)
from media_publisher.sources.quotes_config import QuotesSourcesConfig, load_quotes_sources_config

from media_publisher.scheduling import (
    MIN_SCHEDULE_LEAD_SECONDS,
    PublishMode,
    facebook_can_schedule,
    facebook_wait_message,
    instagram_is_due,
    instagram_wait_message,
    publish_local_date,
)

QUOTE_PLATFORMS: tuple[PlatformName, ...] = ("youtube", "facebook", "instagram")


@dataclass(frozen=True)
class QuotesPipelineSettings:
    work_dir: Path
    project_root: Path
    quotes_sources_config: Path
    google_service_account: Path
    publish_timezone: str
    publish_hour: int
    template_urls: dict[str, str]
    meta_page_id: str
    meta_instagram_account_id: str
    meta_access_token: str
    meta_app_id: str | None
    youtube_client_secrets: Path
    youtube_token: Path
    youtube_channel_handle: str
    youtube_playlist_title: str
    youtube_playlist_id: str | None
    ffmpeg_path: str | None
    publish_mode: PublishMode = "staggered"
    private_test: bool = False
    reference_date: date | None = None
    platforms: tuple[PlatformName, ...] | None = None
    youtube_daily_playlist_id: str | None = None
    youtube_daily_playlist_slots_path: Path | None = None


def filter_quotes_for_local_date(
    posts: list[LocalQuotePost],
    target_date: date,
    *,
    publish_timezone: str,
) -> list[LocalQuotePost]:
    return [
        post
        for post in posts
        if publish_local_date(post.publish_at, publish_timezone) == target_date
    ]


def quote_is_due(post: LocalQuotePost, *, now: datetime | None = None) -> bool:
    current = now or datetime.now(timezone.utc)
    publish_at = post.publish_at
    if publish_at.tzinfo is None:
        publish_at = publish_at.replace(tzinfo=timezone.utc)
    return (publish_at - current).total_seconds() >= MIN_SCHEDULE_LEAD_SECONDS


def pending_platforms(
    post: LocalQuotePost,
    state: dict[str, dict[str, object]],
    *,
    platforms: tuple[PlatformName, ...] | None = None,
) -> list[PlatformName]:
    entry = state.get(post.stem, {})
    pending: list[PlatformName] = []
    for platform in QUOTE_PLATFORMS:
        if platforms is not None and platform not in platforms:
            continue
        if platform_permalink(entry, platform) is None:
            pending.append(platform)
    return pending


def resolve_quote_month(
    reference_date: date | None,
    *,
    publish_timezone: str,
) -> tuple[int, int]:
    if reference_date is not None:
        return reference_date.year, reference_date.month

    from datetime import datetime

    from media_publisher.timezones import get_timezone

    today = datetime.now(get_timezone(publish_timezone)).date()
    return today.year, today.month


def quote_work_items(
    posts: list[LocalQuotePost],
    *,
    settings: QuotesPipelineSettings,
) -> list[tuple[LocalQuotePost, tuple[PlatformName, ...], bool]]:
    """Return (post, platforms, publish_immediately) work items for this run."""
    allowed = settings.platforms
    if settings.publish_mode == "staggered":
        if settings.reference_date is None:
            raise ValueError("reference_date is required for staggered quote publish")
        today = settings.reference_date
        tomorrow = today + timedelta(days=1)
        items: list[tuple[LocalQuotePost, tuple[PlatformName, ...], bool]] = []
        for post in filter_quotes_for_local_date(
            posts, today, publish_timezone=settings.publish_timezone
        ):
            platforms = ("instagram",)
            if allowed is not None:
                platforms = tuple(p for p in platforms if p in allowed)
            if platforms:
                items.append((post, platforms, True))
        for post in filter_quotes_for_local_date(
            posts, tomorrow, publish_timezone=settings.publish_timezone
        ):
            platforms = ("youtube", "facebook")
            if allowed is not None:
                platforms = tuple(p for p in platforms if p in allowed)
            if platforms:
                items.append((post, platforms, False))
        return items

    filtered = posts
    if settings.reference_date is not None:
        filtered = filter_quotes_for_local_date(
            posts,
            settings.reference_date,
            publish_timezone=settings.publish_timezone,
        )
    immediate = settings.publish_mode == "immediate"
    platforms = QUOTE_PLATFORMS if allowed is None else allowed
    return [(post, platforms, immediate) for post in filtered]


def quotes_need_instagram_images(settings: QuotesPipelineSettings) -> bool:
    if settings.private_test:
        return False
    if settings.platforms is not None and "instagram" not in settings.platforms:
        return False
    return True


def run_quotes_pipeline(
    settings: QuotesPipelineSettings,
    *,
    meta_client: MetaClient,
    sheets_client: GoogleSheetsClient | None = None,
    drive_client: GoogleDriveClient | None = None,
    quotes_config: QuotesSourcesConfig | None = None,
    print_line: Callable[[str], None] = print,
) -> tuple[int, list[PlatformPublishResult]]:
    """Render quotes from Google Sheet + Drive backgrounds and schedule/publish them."""
    try:
        synced_slots = flush_configured_daily_playlist(
            client_secrets_path=settings.youtube_client_secrets,
            token_path=settings.youtube_token,
            expected_channel_handle=settings.youtube_channel_handle,
            daily_playlist_id=settings.youtube_daily_playlist_id,
            daily_playlist_slots_path=settings.youtube_daily_playlist_slots_path,
        )
        if synced_slots:
            print_line(
                "Daily playlist updated for public slots: "
                + ", ".join(synced_slots)
            )
    except Exception as exc:
        print_line(f"Daily playlist flush skipped: {exc}")

    year, month = resolve_quote_month(
        settings.reference_date,
        publish_timezone=settings.publish_timezone,
    )

    config = quotes_config or load_quotes_sources_config(settings.quotes_sources_config)
    if sheets_client is None:
        sheets_client = GoogleSheetsClient.from_service_account(settings.google_service_account)
    if drive_client is None:
        drive_client = GoogleDriveClient.from_service_account(settings.google_service_account)

    try:
        posts, ig_images_by_stem = prepare_quote_posts_for_publish(
            config=config,
            sheets_client=sheets_client,
            drive_client=drive_client,
            year=year,
            month=month,
            publish_timezone=settings.publish_timezone,
            publish_hour=settings.publish_hour,
            publish_mode=settings.publish_mode,
            reference_date=settings.reference_date,
        )
    except QuotesRenderPipelineError as exc:
        print_line(f"Failed to prepare quote images: {exc}")
        return 1, []

    if not posts:
        print_line(f"No quote posts found for {year}-{month:02d}.")
        return 0, []

    print_line(
        f"Using rendered quotes from Google Sheet + Drive backgrounds "
        f"({len(posts)} day(s) prepared for {year}-{month:02d})."
    )

    if settings.private_test:
        print_line(
            "Private test: schedule public YouTube and Facebook quote posts for the "
            "next publish slot. Instagram skipped."
        )
    elif settings.publish_mode == "staggered":
        print_line(
            "Staggered publish: today's quote to Instagram immediately; "
            "tomorrow's quote scheduled on YouTube and Facebook for review."
        )
    elif settings.publish_mode == "immediate":
        print_line(
            "Daily quote images are converted to short videos for YouTube. "
            "Facebook and Instagram use the rendered image. Publishing immediately."
        )
    else:
        print_line(
            "Daily quote images are converted to short videos for YouTube (scheduled via API). "
            "Facebook and Instagram use the rendered image (Instagram is published "
            "automatically near the scheduled time)."
        )

    try:
        work_items = quote_work_items(posts, settings=settings)
    except ValueError as exc:
        print_line(str(exc))
        return 1, []

    if not work_items:
        if settings.publish_mode == "staggered" and settings.reference_date is not None:
            tomorrow = (settings.reference_date + timedelta(days=1)).isoformat()
            print_line(
                "No quote posts ready for staggered publish "
                f"(Instagram today {settings.reference_date.isoformat()}, "
                f"YouTube/Facebook tomorrow {tomorrow})."
            )
        elif settings.reference_date is not None:
            print_line(
                f"No quote posts found for {settings.reference_date.isoformat()}."
            )
        else:
            print_line("No quote posts ready to publish.")
        return 0, []

    if quotes_need_instagram_images(settings) and not ig_images_by_stem:
        print_line("Warning: no Instagram quote renders were prepared.")

    state = load_quote_state(settings.work_dir)
    results: list[PlatformPublishResult] = []
    processed_any = False
    quote_video_dir = settings.work_dir / QUOTE_VIDEO_DIRNAME

    for post, target_platforms, publish_immediately in work_items:
        due_platforms = pending_platforms(post, state, platforms=target_platforms)
        if not due_platforms:
            continue
        if (
            not publish_immediately
            and settings.publish_mode == "scheduled"
            and not quote_is_due(post)
        ):
            print_line(
                f"Skipping {post.stem}: publish time "
                f"{post.publish_at.isoformat()} is too soon or already passed."
            )
            continue

        publish_at = None if publish_immediately else post.publish_at

        caption_note = "with caption" if post.caption else "image only"
        print_line(
            f"Processing {post.stem}\t{post.publish_at.isoformat()}\t{caption_note}"
        )
        processed_any = True

        for platform in due_platforms:
            if settings.private_test and platform == "instagram":
                print_line("  instagram: skipped during private test (--private)")
                continue
            if (
                not publish_immediately
                and settings.publish_mode == "scheduled"
                and platform == "instagram"
                and not instagram_is_due(post.publish_at)
            ):
                print_line(f"  instagram: {instagram_wait_message(post.publish_at)}")
                continue
            if (
                not publish_immediately
                and platform == "facebook"
                and not facebook_can_schedule(post.publish_at)
            ):
                print_line(f"  facebook: {facebook_wait_message(post.publish_at)}")
                continue

            if platform == "instagram":
                image_path = ig_images_by_stem.get(post.stem)
                if image_path is None:
                    print_line(
                        f"  instagram: skipped — no rendered IG image for {post.stem}"
                    )
                    continue
            else:
                image_path = post.image_path

            try:
                quote_publish_at = publish_at

                permalink = publish_local_quote(
                    image_path=image_path,
                    caption=post.caption,
                    publish_at=quote_publish_at,
                    private=False,
                    platform=platform,
                    page_id=settings.meta_page_id,
                    instagram_account_id=settings.meta_instagram_account_id,
                    access_token=settings.meta_access_token,
                    meta_client=meta_client,
                    meta_app_id=settings.meta_app_id,
                    youtube_client_secrets=settings.youtube_client_secrets,
                    youtube_token=settings.youtube_token,
                    youtube_channel_handle=settings.youtube_channel_handle,
                    youtube_work_dir=quote_video_dir,
                    youtube_playlist_id=settings.youtube_playlist_id,
                    youtube_playlist_title=settings.youtube_playlist_title,
                    youtube_daily_playlist_id=settings.youtube_daily_playlist_id,
                    youtube_daily_playlist_slots_path=settings.youtube_daily_playlist_slots_path,
                    ffmpeg_path=settings.ffmpeg_path,
                    template_urls=settings.template_urls,
                )
                mark_platform_scheduled_in_state(
                    state,
                    image_name=post.stem,
                    platform=platform,
                    permalink=permalink,
                    publish_at=post.publish_at,
                    source_pdf=post.image_path.name,
                )
                save_quote_state(settings.work_dir, state)
                when = "now" if publish_immediately else post.publish_at.isoformat()
                if publish_immediately:
                    mode = "published"
                else:
                    mode = "scheduled"
                print_line(f"  {platform}: {mode} for {when} ({permalink})")
                results.append(
                    PlatformPublishResult(
                        record_id=post.stem,
                        platform=platform,
                        title=post.caption or post.stem,
                        permalink=permalink,
                    )
                )
            except (
                YouTubePublishError,
                FacebookPublishError,
                InstagramPublishError,
                MetaError,
                QuotesRenderPipelineError,
            ) as exc:
                message = str(exc)
                print_line(f"  {platform}: failed — {message}")
                results.append(
                    PlatformPublishResult(
                        record_id=post.stem,
                        platform=platform,
                        title=post.caption or post.stem,
                        error=message,
                    )
                )

    if not processed_any:
        print_line(
            "No quote posts ready to publish."
            if settings.publish_mode == "immediate"
            else "No quote posts ready to schedule."
        )
        return 0, results

    failures = [result for result in results if not result.success]
    successes = [result for result in results if result.success]
    action = "published" if settings.publish_mode == "immediate" else "scheduled/published"
    print_line(
        f"Done: {len(successes)} {action}, {len(failures)} failed "
        f"across {len({result.record_id for result in results})} day(s)."
    )
    return (1 if failures else 0), results
