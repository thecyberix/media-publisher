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
from media_publisher.publishers.youtube import YouTubePublishError
from media_publisher.sources.canva import CanvaClient, CanvaError, ensure_monthly_quotes_pdf
from media_publisher.sources.quote_pdf import QuotePdfError
from media_publisher.sources.quotes import (
    LocalQuotePost,
    QUOTE_IG_RENDER_DIRNAME,
    QUOTE_VIDEO_DIRNAME,
    discover_monthly_quotes,
    load_quote_state,
    mark_platform_scheduled_in_state,
    platform_permalink,
    quote_canva_design_title,
    quote_canva_ig_design_title,
    save_quote_state,
)

from media_publisher.scheduling import (
    MIN_SCHEDULE_LEAD_SECONDS,
    PRIVATE_TEST_FACEBOOK_SCHEDULE_LEAD_DAYS,
    PublishMode,
    facebook_can_schedule,
    facebook_wait_message,
    instagram_is_due,
    instagram_wait_message,
    private_test_facebook_publish_at,
    publish_local_date,
)

QUOTE_PLATFORMS: tuple[PlatformName, ...] = ("youtube", "facebook", "instagram")


@dataclass(frozen=True)
class QuotesPipelineSettings:
    work_dir: Path
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
    canva_quotes_design_id: str | None = None
    canva_quotes_folder_id: str | None = None
    publish_mode: PublishMode = "staggered"
    private_test: bool = False
    reference_date: date | None = None
    platforms: tuple[PlatformName, ...] | None = None


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


def quotes_need_instagram_design(
    settings: QuotesPipelineSettings,
) -> bool:
    if settings.platforms is not None and "instagram" not in settings.platforms:
        return False
    if settings.private_test and settings.publish_mode == "immediate":
        return False
    return True


def load_instagram_quote_images_by_stem(
    *,
    canva_client: CanvaClient,
    settings: QuotesPipelineSettings,
    year: int,
    month: int,
    print_line: Callable[[str], None],
) -> dict[str, Path]:
    ig_design_title = quote_canva_ig_design_title(year, month)
    try:
        ig_pdf_path = ensure_monthly_quotes_pdf(
            canva_client,
            settings.work_dir,
            year=year,
            month=month,
            design_title=ig_design_title,
            quotes_folder_id=settings.canva_quotes_folder_id,
            variant="ig",
        )
    except CanvaError as exc:
        print_line(
            f"Failed to download Instagram quotes PDF ({ig_design_title}): {exc}"
        )
        return {}

    print_line(f"Using Instagram quotes PDF: {ig_pdf_path.name} ({ig_design_title})")
    try:
        ig_posts = discover_monthly_quotes(
            ig_pdf_path,
            year=year,
            month=month,
            work_dir=settings.work_dir,
            publish_timezone=settings.publish_timezone,
            publish_hour=settings.publish_hour,
            render_dirname=QUOTE_IG_RENDER_DIRNAME,
        )
    except QuotePdfError as exc:
        print_line(f"Failed to read Instagram quotes PDF: {exc}")
        return {}

    return {post.stem: post.image_path for post in ig_posts}


def run_quotes_pipeline(
    settings: QuotesPipelineSettings,
    *,
    meta_client: MetaClient,
    canva_client: CanvaClient,
    print_line: Callable[[str], None] = print,
) -> tuple[int, list[PlatformPublishResult]]:
    """Download the monthly Canva PDF and schedule/publish daily quotes."""
    year, month = resolve_quote_month(
        settings.reference_date,
        publish_timezone=settings.publish_timezone,
    )
    design_title = quote_canva_design_title(year, month)

    try:
        pdf_path = ensure_monthly_quotes_pdf(
            canva_client,
            settings.work_dir,
            year=year,
            month=month,
            design_title=design_title,
            design_id=settings.canva_quotes_design_id,
            quotes_folder_id=settings.canva_quotes_folder_id,
        )
    except CanvaError as exc:
        print_line(f"Failed to download monthly quotes PDF ({design_title}): {exc}")
        return 1, []

    print_line(f"Using monthly quotes PDF: {pdf_path.name} ({design_title})")

    try:
        posts = discover_monthly_quotes(
            pdf_path,
            year=year,
            month=month,
            work_dir=settings.work_dir,
            publish_timezone=settings.publish_timezone,
            publish_hour=settings.publish_hour,
        )
    except QuotePdfError as exc:
        print_line(f"Failed to read monthly quotes PDF: {exc}")
        return 1, []

    if not posts:
        print_line(f"No quote pages found in {pdf_path.name}.")
        return 0, []

    ig_images_by_stem: dict[str, Path] = {}
    if quotes_need_instagram_design(settings):
        ig_images_by_stem = load_instagram_quote_images_by_stem(
            canva_client=canva_client,
            settings=settings,
            year=year,
            month=month,
            print_line=print_line,
        )

    if settings.private_test:
        print_line(
            "Private test: quotes go to YouTube (private) and Facebook "
            f"(scheduled {PRIVATE_TEST_FACEBOOK_SCHEDULE_LEAD_DAYS} days ahead). "
            "Instagram skipped."
        )
    elif settings.publish_mode == "staggered":
        print_line(
            "Staggered publish: today's quote to Instagram immediately; "
            "tomorrow's quote scheduled on YouTube and Facebook for review."
        )
    elif settings.publish_mode == "immediate":
        print_line(
            "Daily quote pages are converted to short videos for YouTube. "
            "Facebook and Instagram use the rendered page image. Publishing immediately."
        )
    else:
        print_line(
            "Daily quote pages are converted to short videos for YouTube (scheduled via API). "
            "Facebook and Instagram use the rendered page image (Instagram is published "
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
                        f"  instagram: skipped — no page found in "
                        f"{quote_canva_ig_design_title(year, month)!r} for {post.stem}"
                    )
                    continue
            else:
                image_path = post.image_path

            try:
                quote_publish_at = publish_at
                if (
                    settings.private_test
                    and publish_immediately
                    and platform == "facebook"
                ):
                    quote_publish_at = private_test_facebook_publish_at()

                permalink = publish_local_quote(
                    image_path=image_path,
                    caption=post.caption,
                    publish_at=quote_publish_at,
                    private=settings.private_test and publish_immediately,
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
                    ffmpeg_path=settings.ffmpeg_path,
                    template_urls=settings.template_urls,
                )
                mark_platform_scheduled_in_state(
                    state,
                    image_name=post.stem,
                    platform=platform,
                    permalink=permalink,
                    publish_at=post.publish_at,
                    source_pdf=post.source_path.name,
                )
                save_quote_state(settings.work_dir, state)
                when = "now" if publish_immediately else post.publish_at.isoformat()
                if publish_immediately:
                    if settings.private_test and platform == "youtube":
                        mode = "published privately"
                    elif settings.private_test and platform == "facebook":
                        mode = "scheduled for test"
                        when = quote_publish_at.isoformat() if quote_publish_at else when
                    else:
                        mode = "published"
                else:
                    mode = "scheduled"
                print_line(f"  {platform}: {mode} for {when} ({permalink})")
                results.append(
                    PlatformPublishResult(
                        record_id=post.stem,
                        platform=platform,
                        title=post.stem,
                        permalink=permalink,
                    )
                )
            except (
                YouTubePublishError,
                FacebookPublishError,
                InstagramPublishError,
                MetaError,
                QuotePdfError,
            ) as exc:
                message = str(exc)
                print_line(f"  {platform}: failed — {message}")
                results.append(
                    PlatformPublishResult(
                        record_id=post.stem,
                        platform=platform,
                        title=post.stem,
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
