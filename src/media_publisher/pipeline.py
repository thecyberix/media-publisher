from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from media_publisher.models import PlatformName, PlatformScheduleTask
from media_publisher.publishers.facebook import FacebookPublishError, publish_to_facebook
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.publishers.youtube import YouTubePublishError, publish_to_youtube, youtube_video_url
from media_publisher.sources.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_TITLE,
    FIELD_TRANSLATION_RESOURCES,
    FIELD_VIDEO_NAME_TRANSLATED,
    fetch_missing_translation_reports,
    fetch_pending_schedule_tasks,
    mark_platform_scheduled,
)
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    HappyScribeError,
    HappyScribeLibraryLocation,
    ensure_catalog_video_downloaded,
)
from media_publisher.sources.canva import CanvaClient, CanvaError
from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError
from media_publisher.sources.publish_media import (
    PublishMediaCleanup,
    merge_publish_media_cleanup,
    apply_publish_media_cleanup,
    resolve_publish_thumbnail,
    resolve_publish_video,
)
from media_publisher.sources.tn_publish import TnPublishSettings
from media_publisher.scheduling import (
    PublishMode,
    prepare_job_for_immediate_publish,
    private_test_facebook_publish_at,
    select_tasks_for_run,
    task_uses_immediate_publish,
)
from media_publisher.sources.happyscribe_web import HappyScribeWebError
from media_publisher.video_duration import (
    instagram_duration_skip_message,
    instagram_exceeds_api_limit,
    resolve_video_duration_seconds,
)


@dataclass(frozen=True)
class PlatformPublishResult:
    record_id: str
    platform: PlatformName
    title: str
    permalink: str | None = None
    error: str | None = None

    @property
    def success(self) -> bool:
        return self.error is None and self.permalink is not None


@dataclass(frozen=True)
class PublishPipelineSettings:
    """Runtime dependencies for the default publish pipeline."""

    project_root: Path
    publish_timezone: str
    publish_hour: int
    canva_download_dir: Path
    canva_client: CanvaClient | None
    canva_long_video_thumbnails_url: str
    canva_short_video_thumbnails_url: str
    happyscribe_download_dir: Path
    happyscribe_browser_state: Path
    happyscribe_browser_profile: Path | None
    happyscribe_browser_channel: str | None
    happyscribe_api_key: str | None
    happyscribe_headless: bool
    ffmpeg_path: str | None
    youtube_client_secrets: Path
    youtube_token: Path
    youtube_channel_handle: str
    youtube_playlist_title: str
    youtube_playlist_id: str | None
    template_urls: dict[str, str]
    meta_page_id: str
    meta_instagram_account_id: str
    meta_access_token: str
    meta_app_id: str | None
    publish_mode: PublishMode = "staggered"
    private_test: bool = False
    reference_date: date | None = None
    regenerate_videos: bool = False
    use_web_export: bool = False
    happyscribe_published_folder_id: str | None = None
    youtube_short_cover_end_seconds: float = 2.0
    skip_thumbnails: bool = False
    publish_override_drive_folder_id: str = ""
    publish_override_thumbnails_subfolder: str = "Thumbnails"
    publish_override_videos_subfolder: str = "Videos"
    canva_published_subfolder_name: str = "Published"
    google_drive_service_account: Path | None = None
    publish_media_download_dir: Path | None = None
    tn_publish_settings: TnPublishSettings | None = None


def group_tasks_by_record(
    tasks: list[PlatformScheduleTask],
) -> dict[str, list[PlatformScheduleTask]]:
    grouped: dict[str, list[PlatformScheduleTask]] = defaultdict(list)
    for task in tasks:
        grouped[task.record_id].append(task)
    for record_tasks in grouped.values():
        record_tasks.sort(key=lambda item: item.platform)
    return dict(grouped)


def catalog_name_from_task(task: PlatformScheduleTask) -> str:
    return task.job.metadata.get(FIELD_TITLE) or task.job.title


def required_platforms(tasks: list[PlatformScheduleTask]) -> set[PlatformName]:
    return {task.platform for task in tasks}


def publish_platform_task(
    task: PlatformScheduleTask,
    *,
    settings: PublishPipelineSettings,
    meta_client: MetaClient,
) -> str:
    if task.platform == "youtube":
        video_id = publish_to_youtube(
            task.job,
            client_secrets_path=settings.youtube_client_secrets,
            token_path=settings.youtube_token,
            expected_channel_handle=settings.youtube_channel_handle,
            ffmpeg_path=settings.ffmpeg_path,
            cover_end_seconds=settings.youtube_short_cover_end_seconds,
            playlist_id=settings.youtube_playlist_id,
            playlist_title=settings.youtube_playlist_title,
            **settings.template_urls,
        )
        return youtube_video_url(video_id)

    if task.platform == "facebook":
        if meta_client is None:
            raise FacebookPublishError("Meta client is not configured")
        post_id = publish_to_facebook(
            task.job,
            page_id=settings.meta_page_id,
            access_token=settings.meta_access_token,
            app_id=settings.meta_app_id,
            **settings.template_urls,
        )
        return meta_client.get_facebook_video_permalink(post_id)

    if task.platform == "instagram":
        if meta_client is None:
            raise InstagramPublishError("Meta client is not configured")
        media_id = publish_to_instagram(
            task.job,
            instagram_account_id=settings.meta_instagram_account_id,
            access_token=settings.meta_access_token,
            app_id=settings.meta_app_id,
            page_id=settings.meta_page_id,
            ffmpeg_path=settings.ffmpeg_path,
            **settings.template_urls,
        )
        return meta_client.get_instagram_media_permalink(media_id)

    raise ValueError(f"Unsupported platform {task.platform!r}")


def run_publish_pipeline(
    airtable: AirtableClient,
    happyscribe: HappyScribeClient,
    happyscribe_location: HappyScribeLibraryLocation,
    settings: PublishPipelineSettings,
    *,
    meta_client: MetaClient | None = None,
    print_line: Callable[[str], None] = print,
) -> tuple[int, list[PlatformPublishResult]]:
    """Fetch pending catalog rows, download videos, publish, and update Airtable."""
    tasks = fetch_pending_schedule_tasks(
        airtable,
        publish_timezone=settings.publish_timezone,
        publish_hour=settings.publish_hour,
        videos_only=True,
    )
    skipped = fetch_missing_translation_reports(
        airtable,
        publish_timezone=settings.publish_timezone,
        publish_hour=settings.publish_hour,
    )
    if skipped:
        print_line(
            f"Skipped — missing {FIELD_VIDEO_NAME_TRANSLATED!r} ({len(skipped)}):"
        )
        for report in skipped:
            platforms = ", ".join(report.platforms)
            print_line(f"{report.record_id}\t{report.original_title}\t{platforms}")

    if not tasks:
        print_line("No pending schedules found.")
        return 0, []

    if settings.reference_date is not None:
        tasks = select_tasks_for_run(
            tasks,
            publish_mode=settings.publish_mode,
            reference_date=settings.reference_date,
            publish_timezone=settings.publish_timezone,
        )
        if not tasks:
            if settings.publish_mode == "staggered":
                tomorrow = (settings.reference_date + timedelta(days=1)).isoformat()
                today = settings.reference_date.isoformat()
                print_line(
                    "No pending schedules found for staggered publish "
                    f"(Instagram today {today}, YouTube/Facebook tomorrow {tomorrow})."
                )
            else:
                print_line(
                    f"No pending schedules found for {settings.reference_date.isoformat()}."
                )
            return 0, []

    platforms_needed = required_platforms(tasks)
    if platforms_needed & {"facebook", "instagram"} and meta_client is None:
        raise RuntimeError("Meta client is required for Facebook and Instagram publishing")

    results: list[PlatformPublishResult] = []
    grouped = group_tasks_by_record(tasks)
    extra_folders = (
        [settings.happyscribe_published_folder_id]
        if settings.happyscribe_published_folder_id
        else []
    )
    library_transcriptions = happyscribe.list_search_transcriptions(
        happyscribe_location,
        extra_folder_ids=extra_folders,
    )

    for record_id, record_tasks in grouped.items():
        ready_tasks = list(record_tasks)
        if not ready_tasks:
            continue

        if settings.private_test:
            skipped_instagram = [
                task for task in ready_tasks if task.platform == "instagram"
            ]
            if skipped_instagram:
                print_line("  instagram: skipped during private test (--private)")
            ready_tasks = [
                task for task in ready_tasks if task.platform != "instagram"
            ]
        if not ready_tasks:
            continue

        catalog_name = catalog_name_from_task(record_tasks[0])
        title = record_tasks[0].job.title
        smartcat_url = record_tasks[0].job.metadata.get(FIELD_TRANSLATION_RESOURCES)
        record_fields = dict(record_tasks[0].record_fields)
        lookup_title = record_fields.get(FIELD_TITLE) or catalog_name
        print_line(f"Processing {record_id}\t{catalog_name}\t{title}")

        drive_client: GoogleDriveClient | None = None
        if settings.google_drive_service_account and settings.google_drive_service_account.exists():
            try:
                drive_client = GoogleDriveClient.from_service_account(
                    settings.google_drive_service_account
                )
            except GoogleDriveError as exc:
                print_line(f"  Drive client unavailable: {exc}")

        media_download_dir = settings.publish_media_download_dir or (
            settings.canva_download_dir.parent / "publish-media"
        )
        media_download_dir.mkdir(parents=True, exist_ok=True)

        publish_cleanup: PublishMediaCleanup | None = None
        video_path: Path | None = None
        thumbnail_path: str | None = None

        if not settings.skip_thumbnails:
            try:
                thumbnail_result = resolve_publish_thumbnail(
                    record_tasks[0].job,
                    record_fields,
                    title=str(lookup_title),
                    canva_client=settings.canva_client,
                    drive=drive_client,
                    canva_download_dir=settings.canva_download_dir,
                    long_catalog_url=settings.canva_long_video_thumbnails_url,
                    short_catalog_url=settings.canva_short_video_thumbnails_url,
                    override_root_folder_id=settings.publish_override_drive_folder_id,
                    thumbnails_subfolder=settings.publish_override_thumbnails_subfolder,
                    published_subfolder_name=settings.canva_published_subfolder_name,
                    tn_settings=settings.tn_publish_settings
                    or TnPublishSettings(
                        original_dir=settings.project_root / "downloads/original-thumbnails",
                        cache_dir=settings.project_root / "downloads/tn-cache",
                        output_dir=settings.project_root / "downloads/tn-rendered",
                        english_override_file=settings.project_root
                        / "downloads/tn-english-overrides.json",
                    ),
                )
                thumbnail_path = (
                    str(thumbnail_result.path) if thumbnail_result.path else None
                )
                publish_cleanup = merge_publish_media_cleanup(
                    publish_cleanup, thumbnail_result.cleanup
                )
                if thumbnail_path:
                    print_line(
                        f"  Thumbnail ({thumbnail_result.source}): {thumbnail_path}"
                    )
            except CanvaError as exc:
                print_line(f"  Thumbnail resolution failed: {exc}")

        if drive_client is not None and settings.publish_override_drive_folder_id:
            video_override = resolve_publish_video(
                title=str(lookup_title),
                drive=drive_client,
                override_root_folder_id=settings.publish_override_drive_folder_id,
                videos_subfolder=settings.publish_override_videos_subfolder,
                download_dir=media_download_dir,
            )
            if video_override.path is not None:
                video_path = video_override.path
                publish_cleanup = merge_publish_media_cleanup(
                    publish_cleanup, video_override.cleanup
                )
                print_line(f"  Video ({video_override.source}): {video_path}")

        if video_path is None:
            try:
                video_path = ensure_catalog_video_downloaded(
                    catalog_name,
                    download_dir=settings.happyscribe_download_dir,
                    client=happyscribe,
                    location=happyscribe_location,
                    browser_state_path=settings.happyscribe_browser_state,
                    browser_profile_dir=settings.happyscribe_browser_profile,
                    browser_channel=settings.happyscribe_browser_channel,
                    api_key=settings.happyscribe_api_key,
                    headless=settings.happyscribe_headless,
                    transcriptions=library_transcriptions,
                    force_regenerate=settings.regenerate_videos,
                    use_web_export=settings.use_web_export,
                    smartcat_url=smartcat_url,
                )
                print_line(f"  Video: {video_path}")
            except (HappyScribeError, HappyScribeWebError) as exc:
                message = str(exc)
                print_line(f"  Video download failed: {message}")
                for task in record_tasks:
                    results.append(
                        PlatformPublishResult(
                            record_id=record_id,
                            platform=task.platform,
                            title=title,
                            error=message,
                        )
                    )
                continue

        duration_seconds = resolve_video_duration_seconds(
            video_path=video_path,
            metadata=record_tasks[0].job.metadata,
        )
        if instagram_exceeds_api_limit(duration_seconds):
            skipped_instagram = [
                task for task in ready_tasks if task.platform == "instagram"
            ]
            if skipped_instagram:
                assert duration_seconds is not None
                print_line(f"  {instagram_duration_skip_message(duration_seconds)}")
            ready_tasks = [
                task for task in ready_tasks if task.platform != "instagram"
            ]

        record_success_count = 0
        for task in ready_tasks:
            task = replace(task, job=replace(task.job))
            task.job.video_path = str(video_path)
            task.job.thumbnail_path = thumbnail_path
            catalog_publish_at = task.publish_at
            uses_immediate = task_uses_immediate_publish(
                task.platform,
                settings.publish_mode,
            )
            if uses_immediate:
                if settings.private_test and task.platform == "facebook":
                    task.job.publish_at = private_test_facebook_publish_at()
                else:
                    prepare_job_for_immediate_publish(
                        task.job,
                        private=settings.private_test
                        and settings.publish_mode == "immediate",
                    )
            try:
                permalink = publish_platform_task(
                    task,
                    settings=settings,
                    meta_client=meta_client,
                )
                updated = mark_platform_scheduled(
                    airtable,
                    record_id=record_id,
                    record_fields=record_fields,
                    platform=task.platform,
                    permalink=permalink,
                )
                record_fields = dict(updated.fields)
                when = (
                    "now"
                    if uses_immediate
                    and not (
                        settings.private_test and task.platform == "facebook"
                    )
                    else task.job.publish_at.isoformat()
                    if settings.private_test and task.platform == "facebook"
                    else catalog_publish_at.isoformat()
                )
                mode = (
                    "published privately"
                    if settings.private_test and task.platform == "youtube"
                    else "scheduled for test"
                    if settings.private_test and task.platform == "facebook"
                    else "published"
                    if uses_immediate
                    else "scheduled"
                )
                print_line(f"  {task.platform}: {mode} for {when} ({permalink})")
                results.append(
                    PlatformPublishResult(
                        record_id=record_id,
                        platform=task.platform,
                        title=title,
                        permalink=permalink,
                    )
                )
                record_success_count += 1
            except (
                AirtableError,
                YouTubePublishError,
                FacebookPublishError,
                InstagramPublishError,
                MetaError,
            ) as exc:
                message = str(exc)
                print_line(f"  {task.platform}: failed — {message}")
                results.append(
                    PlatformPublishResult(
                        record_id=record_id,
                        platform=task.platform,
                        title=title,
                        error=message,
                    )
                )

        if record_success_count == len(ready_tasks) and publish_cleanup is not None:
            apply_publish_media_cleanup(
                publish_cleanup,
                drive=drive_client,
                canva_client=settings.canva_client,
                log=print_line,
            )

    failures = [result for result in results if not result.success]
    successes = [result for result in results if result.success]
    print_line(
        f"Done: {len(successes)} published, {len(failures)} failed "
        f"({len(grouped)} record(s))."
    )
    return (1 if failures else 0), results
