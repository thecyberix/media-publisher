from __future__ import annotations

import argparse
import os
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from media_publisher.config import load_settings, update_env_values
from media_publisher.runtime_env import maybe_persist_canva_token
from media_publisher.models import PlatformName
from media_publisher.scheduling import (
    instagram_is_due,
    instagram_wait_message,
    next_catalog_publish_at,
    publish_local_date,
)
from media_publisher.video_duration import (
    instagram_duration_skip_message,
    instagram_exceeds_api_limit,
    resolve_video_duration_seconds,
)
from media_publisher.pipeline import PublishPipelineSettings, run_publish_pipeline
from media_publisher.sources.publish_media import (
    PublishMediaCleanup,
    apply_publish_media_cleanup,
    merge_publish_media_cleanup,
    resolve_publish_thumbnail,
    resolve_publish_video,
)
from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError
from media_publisher.sources.tn_publish import TnPublishError, TnPublishSettings
from media_publisher.quotes_pipeline import QuotesPipelineSettings, run_quotes_pipeline
from media_publisher.sources.quote_pdf import QuotePdfError
from media_publisher.publishers.facebook import FacebookPublishError, publish_to_facebook
from media_publisher.publishers.instagram import InstagramPublishError, publish_to_instagram
from media_publisher.publishers.meta import (
    MetaClient,
    MetaError,
    MetaPageInfo,
    inspect_access_token,
    normalize_facebook_page_username,
    normalize_instagram_username,
    resolve_permanent_page_token,
)
from media_publisher.publishers.youtube import (
    YouTubeClient,
    YouTubePublishError,
    publish_to_youtube,
    youtube_video_url,
)
from media_publisher.analytics.channel_report import (
    ChannelReportError,
    _enabled_platforms,
    ensure_report_write_ranges_unprotected,
    inspect_channel_report_sheet,
    load_channel_report_mapping,
    parse_month_cell,
    update_channel_report,
)
from media_publisher.analytics.channel_report_snapshots import (
    ChannelReportSnapshotError,
    capture_channel_report_snapshots,
)
from media_publisher.sources.google_sheets import GoogleSheetsClient, GoogleSheetsError
from media_publisher.sources.airtable import (
    AirtableClient,
    AirtableError,
    FIELD_TITLE,
    FIELD_VIDEO_NAME_TRANSLATED,
    STATUS_SYNC_DONE,
    fetch_missing_translation_reports,
    fetch_pending_schedule_tasks,
    has_video_name_translated,
    mark_platform_scheduled,
    mark_record_done_and_published_if_complete,
    record_schedule_tasks,
    record_to_publish_job,
    STATUS_DONE_PUBLISHED,
)
from media_publisher.sources.canva import (
    DEFAULT_SCOPES,
    CanvaClient,
    CanvaError,
    download_images_from_canva_url,
    ensure_catalog_thumbnail_from_canva,
    format_access_token_scopes,
    missing_canva_scopes,
    parse_canva_resource,
    parse_design_id,
    resolve_canva_url,
)
from media_publisher.sources.happyscribe import (
    HappyScribeClient,
    HappyScribeError,
    TRANSCRIPTION_STATE_READY,
    burned_video_destination_path,
    find_downloaded_video,
    is_subtitled_export_name,
    resolve_library_location,
)
from media_publisher.sources.happyscribe_web import (
    HappyScribeWebError,
    export_video_with_subtitles_web,
    import_browser_session,
    save_browser_session_interactive,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def configure_console_output() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def print_console(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Extract publishing metadata from Airtable, HappyScribe, and Canva, "
            "then publish to YouTube, Facebook, and Instagram. "
            "Run without flags to process all pending catalog entries."
        )
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate required environment variables and exit.",
    )
    parser.add_argument(
        "--test-airtable",
        action="store_true",
        help="Verify Airtable credentials by reading up to one record.",
    )
    parser.add_argument(
        "--test-happyscribe",
        action="store_true",
        help="Verify HappyScribe credentials by listing organizations and transcriptions.",
    )
    parser.add_argument(
        "--list-happyscribe-library",
        action="store_true",
        help="List videos in the configured HappyScribe library folder.",
    )
    parser.add_argument(
        "--download-happyscribe-library",
        action="store_true",
        help=(
            "Download ready source videos from the configured HappyScribe library folder "
            "and burn in subtitles locally with ffmpeg."
        ),
    )
    parser.add_argument(
        "--export-happyscribe-web",
        metavar="TRANSCRIPTION_ID",
        help="Export one HappyScribe video with styled burned-in subtitles via the web session.",
    )
    parser.add_argument(
        "--happyscribe-web-export",
        action="store_true",
        help=(
            "Use the HappyScribe web exporter for styled subtitles instead of the "
            "default ffmpeg burn when downloading videos in the publish pipeline."
        ),
    )
    parser.add_argument(
        "--happyscribe-export-headless",
        action="store_true",
        help="Run the HappyScribe web export browser in headless mode.",
    )
    parser.add_argument(
        "--happyscribe-save-session",
        action="store_true",
        help="Open HappyScribe in Chrome/Edge and save an authenticated session for web export.",
    )
    parser.add_argument(
        "--happyscribe-import-session",
        metavar="PATH",
        help="Import a Playwright storage-state JSON file exported from a normal browser login.",
    )
    parser.add_argument(
        "--burn-happyscribe-video",
        metavar="TRANSCRIPTION_ID",
        help=(
            "Download one HappyScribe video via API and burn in subtitles locally with ffmpeg."
        ),
    )
    parser.add_argument(
        "--canva-auth",
        action="store_true",
        help="Print the Canva OAuth authorization URL and save pending PKCE state.",
    )
    parser.add_argument(
        "--canva-auth-code",
        metavar="CODE",
        help="Exchange a Canva authorization code for a token (after --canva-auth).",
    )
    parser.add_argument(
        "--canva-auth-state",
        metavar="STATE",
        help="Optional state value returned by Canva during OAuth.",
    )
    parser.add_argument(
        "--test-canva",
        action="store_true",
        help="Verify Canva credentials by refreshing the stored OAuth token.",
    )
    parser.add_argument(
        "--canva-download",
        metavar="URL",
        help="Download image(s) from a Canva design URL or canva.link short link.",
    )
    parser.add_argument(
        "--canva-format",
        choices=("png", "jpg", "pdf"),
        default="png",
        help="Export format for --canva-download (default: png).",
    )
    parser.add_argument(
        "--canva-split-pages",
        action="store_true",
        help="Export each page as a separate file (needed for per-page PDFs).",
    )
    parser.add_argument(
        "--canva-resolve",
        metavar="URL",
        help="Resolve a canva.link short URL and print the design ID (no API auth needed).",
    )
    parser.add_argument(
        "--youtube-auth",
        action="store_true",
        help="Print the YouTube OAuth authorization URL and save pending state.",
    )
    parser.add_argument(
        "--youtube-auth-code",
        metavar="CODE",
        help="Exchange a YouTube authorization code for a token (after --youtube-auth).",
    )
    parser.add_argument(
        "--youtube-auth-state",
        metavar="STATE",
        help="Optional state value returned by Google during OAuth.",
    )
    parser.add_argument(
        "--test-youtube",
        action="store_true",
        help="Verify YouTube credentials by refreshing the stored OAuth token.",
    )
    parser.add_argument(
        "--test-meta",
        action="store_true",
        help="Verify Meta credentials for the configured Sadhguru Bulgarian accounts.",
    )
    parser.add_argument(
        "--resolve-meta",
        action="store_true",
        help="Resolve Facebook Page and Instagram account IDs from configured usernames.",
    )
    parser.add_argument(
        "--meta-setup-token",
        metavar="TOKEN",
        nargs="?",
        const="__USE_ENV__",
        help=(
            "Exchange a long-lived user token for a permanent Page token and save it to .env. "
            "Omit TOKEN to use META_USER_ACCESS_TOKEN from the environment."
        ),
    )
    parser.add_argument(
        "--list-pending",
        action="store_true",
        help=(
            "List Airtable videos with Status 'Synchronization done' that have a "
            "platform publish date set but no published permalink yet."
        ),
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help=(
            "Use native platform scheduling (YouTube/Facebook schedule APIs) instead of "
            "publishing immediately. Still only processes entries due today. Instagram "
            "is published near the scheduled time."
        ),
    )
    parser.add_argument(
        "--publish-today",
        action="store_true",
        help=(
            "Default behavior: publish immediately all pending videos or quote posts whose "
            "publish date is today (in the configured publish timezone). Kept for "
            "compatibility; omit this flag for the same result."
        ),
    )
    parser.add_argument(
        "--private",
        action="store_true",
        help=(
            "Schedule public YouTube and Facebook posts for the next publish slot "
            "(today or tomorrow depending on time). Skips the Instagram upload only; "
            "publish dates and status are unchanged. Instagram uploads happen on a "
            "normal run when due (or are skipped automatically above 15 minutes)."
        ),
    )
    parser.add_argument(
        "--regenerate-videos",
        action="store_true",
        help=(
            "Re-download videos from HappyScribe instead of reusing cached local files. "
            "Subtitles are burned in with ffmpeg by default."
        ),
    )
    parser.add_argument(
        "--schedule-youtube",
        metavar="RECORD_ID",
        help="Schedule or publish a video to YouTube from an Airtable record.",
    )
    parser.add_argument(
        "--schedule-facebook",
        metavar="RECORD_ID",
        help="Schedule or publish a video to Facebook from an Airtable record.",
    )
    parser.add_argument(
        "--schedule-instagram",
        metavar="RECORD_ID",
        help="Schedule or publish a Reel to Instagram from an Airtable record.",
    )
    parser.add_argument(
        "--quotes",
        action="store_true",
        help=(
            "Schedule or publish today's quote from the monthly Canva PDF in "
            "downloads/canva. The design is resolved by title in the DMQ templates "
            "folder, e.g. 'Юли 2026 FB/YT DMQ Template Final' (Instagram uses "
            "'Юли 2026 IG DMQ Template Final'). "
            "Each PDF page is one day of the month (page N = day N). Images become short "
            "videos for YouTube (scheduled Short with image thumbnail). Facebook and "
            "Instagram use the rendered page image (Instagram is published automatically "
            "near the scheduled time with --watch). Use --platform to limit platforms."
        ),
    )
    parser.add_argument(
        "--platform",
        action="append",
        choices=["youtube", "facebook", "instagram"],
        metavar="PLATFORM",
        help=(
            "Publish only to PLATFORM (youtube, facebook, or instagram). "
            "Repeat for multiple platforms. Default: all still-pending platforms. "
            "Applies to the default video pipeline, --watch, --list-pending, and --quotes."
        ),
    )
    parser.add_argument(
        "--skip-thumbnails",
        action="store_true",
        help=(
            "Skip downloading video thumbnails from Canva (use cached local thumbnails only). "
            "Useful when running locally to avoid Canva OAuth token refresh."
        ),
    )
    parser.add_argument(
        "--fix-channel-report-protection",
        action="store_true",
        help=(
            "Extend sheet-wide protection holes on the Bulgarian tab so Views Actual "
            "rows can be updated by the service account."
        ),
    )
    parser.add_argument(
        "--channel-report-all-months",
        action="store_true",
        help=(
            "Update every month column through the last complete calendar month. "
            "Default: only the last complete month."
        ),
    )
    parser.add_argument(
        "--channel-report-month",
        metavar="YYYY-MM",
        help="Update a single report month, e.g. 2026-02.",
    )
    parser.add_argument(
        "--inspect-channel-report",
        action="store_true",
        help=(
            "Print the configured Bulgarian channel report sheet layout "
            "(requires Google Sheets service account access)."
        ),
    )
    parser.add_argument(
        "--update-channel-report",
        action="store_true",
        help=(
            "Fetch monthly YouTube/Facebook/Instagram views and write them into "
            "the configured Google Sheet report tab."
        ),
    )
    parser.add_argument(
        "--channel-report-recent-months",
        action="store_true",
        help=(
            "Update the last complete month and the current in-progress month. "
            "Useful for weekly refreshes without backfilling older months."
        ),
    )
    parser.add_argument(
        "--channel-report-snapshot",
        action="store_true",
        help=(
            "Capture follower/subscriber counts into the local snapshot store. "
            "Run daily so short-retention Meta metrics are not lost."
        ),
    )
    parser.add_argument(
        "--dry-run-channel-report",
        action="store_true",
        help="Show channel report updates without writing to Google Sheets.",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        const=5,
        type=int,
        metavar="MINUTES",
        help=(
            "Run the default publish pipeline every N minutes (default: 5). "
            "Keeps Instagram posts on schedule without manual runs."
        ),
    )
    return parser


def airtable_client_from_settings(settings) -> AirtableClient:
    return AirtableClient(
        token=settings.airtable_token,
        base_id=settings.airtable_base_id,
        table_name=settings.airtable_table_name,
        api_base=settings.airtable_api_base,
        view=settings.airtable_view,
    )


def happyscribe_client_from_settings(settings) -> HappyScribeClient:
    return HappyScribeClient(
        api_key=settings.happyscribe_api_key or "",
        api_base=settings.happyscribe_api_base,
        organization_id=settings.happyscribe_organization_id,
        ffmpeg_path=settings.happyscribe_ffmpeg,
    )


def happyscribe_library_from_settings(settings):
    return resolve_library_location(
        library_url=settings.happyscribe_library_url,
        organization_id=settings.happyscribe_organization_id,
        folder_id=settings.happyscribe_folder_id,
    )


def happyscribe_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.happyscribe_api_key:
        missing.append("HAPPYSCRIBE_API_KEY")
    return missing


def happyscribe_browser_state_path(settings) -> Path:
    return PROJECT_ROOT / settings.happyscribe_browser_state


def happyscribe_browser_profile_path(settings) -> Path:
    return PROJECT_ROOT / settings.happyscribe_browser_profile


def happyscribe_web_export_kwargs(settings, args) -> dict:
    return {
        "browser_state_path": happyscribe_browser_state_path(settings),
        "browser_profile_dir": happyscribe_browser_profile_path(settings),
        "browser_channel": settings.happyscribe_browser_channel,
        "api_key": settings.happyscribe_api_key,
        "headless": args.happyscribe_export_headless,
    }


def happyscribe_web_settings_missing(settings) -> list[str]:
    missing = happyscribe_settings_missing(settings)
    if not happyscribe_browser_state_path(settings).exists():
        missing.append(f"browser session ({settings.happyscribe_browser_state})")
    return missing


def happyscribe_library_settings_missing(settings) -> list[str]:
    missing = happyscribe_settings_missing(settings)
    if not (
        settings.happyscribe_library_url
        or (settings.happyscribe_organization_id and settings.happyscribe_folder_id)
    ):
        missing.append(
            "HAPPYSCRIBE_LIBRARY_URL or (HAPPYSCRIBE_ORGANIZATION_ID + HAPPYSCRIBE_FOLDER_ID)"
        )
    return missing


def canva_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.canva_client_id:
        missing.append("CANVA_CLIENT_ID")
    if not settings.canva_client_secret:
        missing.append("CANVA_CLIENT_SECRET")
    if not (PROJECT_ROOT / settings.canva_token).exists():
        missing.append(f"token file ({settings.canva_token})")
    return missing


def canva_client_from_settings(settings) -> CanvaClient:
    return CanvaClient(
        client_id=settings.canva_client_id or "",
        client_secret=settings.canva_client_secret or "",
        token_path=PROJECT_ROOT / settings.canva_token,
        api_base=settings.canva_api_base,
        redirect_uri=settings.canva_redirect_uri,
    )


def canva_settings_complete(settings) -> bool:
    return bool(
        settings.canva_client_id
        and settings.canva_client_secret
        and (PROJECT_ROOT / settings.canva_token).exists()
    )


def meta_settings_complete(settings) -> bool:
    return bool(settings.meta_access_token)


def meta_client_from_settings(settings) -> MetaClient:
    return MetaClient(
        settings.meta_access_token or "",
        api_version=settings.meta_api_version,
        app_id=settings.meta_app_id,
    )


def meta_settings_missing(settings) -> list[str]:
    missing = []
    if not settings.meta_access_token:
        missing.append("META_ACCESS_TOKEN")
    if not settings.meta_app_id:
        missing.append("META_APP_ID")
    return missing


def meta_facebook_url(settings) -> str:
    username = normalize_facebook_page_username(settings.meta_page_username)
    return f"https://www.facebook.com/{username}"


def meta_instagram_url(settings) -> str:
    username = normalize_instagram_username(settings.meta_instagram_username)
    return f"https://www.instagram.com/{username}/"


def resolve_meta_targets(settings) -> tuple[str, str, MetaPageInfo]:
    client = meta_client_from_settings(settings)
    page_info = client.resolve_page_by_username(settings.meta_page_username)
    client.verify_instagram_username(page_info, settings.meta_instagram_username)

    if settings.meta_page_id and settings.meta_page_id != page_info.page_id:
        raise MetaError(
            f"META_PAGE_ID {settings.meta_page_id!r} does not match "
            f"Facebook page {settings.meta_page_username!r}"
        )

    page_id = settings.meta_page_id or page_info.page_id
    instagram_account_id = settings.meta_instagram_account_id or page_info.instagram_account_id
    if not instagram_account_id:
        raise MetaError(
            "No Instagram business account is linked to the Facebook page. "
            f"Connect {meta_instagram_url(settings)} in Meta Business Suite."
        )

    if (
        settings.meta_instagram_account_id
        and page_info.instagram_account_id
        and settings.meta_instagram_account_id != page_info.instagram_account_id
    ):
        raise MetaError(
            f"META_INSTAGRAM_ACCOUNT_ID {settings.meta_instagram_account_id!r} does not match "
            f"the account linked to {settings.meta_page_username!r}"
        )

    return page_id, instagram_account_id, page_info


def canva_download_dir_from_settings(settings) -> Path:
    return PROJECT_ROOT / settings.canva_download_dir


def resolve_canva_quotes_folder_id(settings) -> str | None:
    value = (settings.canva_quotes_folder_id or "").strip()
    if not value:
        return None
    resource_type, resource_id = parse_canva_resource(value)
    if resource_type != "folder":
        raise CanvaError(
            "CANVA_QUOTES_FOLDER_ID must be a Canva folder URL or folder id"
        )
    return resource_id


def template_urls_from_settings(settings) -> dict[str, str]:
    return {
        "facebook_url": meta_facebook_url(settings),
        "instagram_url": meta_instagram_url(settings),
        "youtube_channel_url": settings.youtube_channel_url,
    }


def publish_schedule_settings(settings):
    return {
        "publish_timezone": settings.publish_timezone,
        "publish_hour": settings.publish_hour,
    }


def load_schedule_task(settings, record_id: str, platform: PlatformName):
    client = airtable_client_from_settings(settings)
    record = client.get_record(record_id)
    original_title = record.fields.get(FIELD_TITLE) or "Untitled"
    if not has_video_name_translated(record.fields):
        raise AirtableError(
            f"Cannot publish {record_id!r} ({original_title!r}): "
            f'"{FIELD_VIDEO_NAME_TRANSLATED}" is empty.'
        )
    schedule = publish_schedule_settings(settings)
    tasks = record_schedule_tasks(
        record,
        platforms=(platform,),
        **schedule,
    )
    if not tasks:
        raise AirtableError(
            f"Record {record_id!r} is not ready to schedule on {platform}: "
            f"requires Status {STATUS_SYNC_DONE!r}, a publish date, and no existing permalink."
        )
    task = tasks[0]
    cleanup: PublishMediaCleanup | None = None
    lookup_title = str(record.fields.get(FIELD_TITLE) or task.job.title)
    drive_client = None
    service_account = PROJECT_ROOT / settings.google_sheets_service_account
    if service_account.exists():
        try:
            drive_client = GoogleDriveClient.from_service_account(service_account)
        except GoogleDriveError:
            drive_client = None

    if not getattr(settings, "skip_thumbnails", False):
        canva_client = None
        if not canva_settings_missing(settings):
            canva_client = canva_client_from_settings(settings)
        try:
            thumbnail_result = resolve_publish_thumbnail(
                task.job,
                dict(record.fields),
                title=lookup_title,
                canva_client=canva_client,
                drive=drive_client,
                canva_download_dir=canva_download_dir_from_settings(settings),
                long_catalog_url=settings.canva_long_video_thumbnails_url,
                short_catalog_url=settings.canva_short_video_thumbnails_url,
                override_root_folder_id=settings.publish_override_drive_folder_id,
                thumbnails_subfolder=settings.publish_override_thumbnails_subfolder,
                published_subfolder_name=settings.canva_published_subfolder_name,
                tn_settings=TnPublishSettings(
                    original_dir=PROJECT_ROOT / settings.tn_original_thumbnail_dir,
                    cache_dir=PROJECT_ROOT / settings.tn_cache_dir,
                    output_dir=PROJECT_ROOT / settings.tn_render_output_dir,
                    english_override_file=PROJECT_ROOT / settings.tn_english_override_file,
                ),
            )
            cleanup = merge_publish_media_cleanup(cleanup, thumbnail_result.cleanup)
            if thumbnail_result.path is not None:
                task = replace(
                    task,
                    job=replace(task.job, thumbnail_path=str(thumbnail_result.path)),
                )
        except (CanvaError, TnPublishError) as exc:
            raise AirtableError(f"Thumbnail lookup failed: {exc}") from exc

    if drive_client is not None and settings.publish_override_drive_folder_id:
        video_override = resolve_publish_video(
            title=lookup_title,
            drive=drive_client,
            override_root_folder_id=settings.publish_override_drive_folder_id,
            videos_subfolder=settings.publish_override_videos_subfolder,
            download_dir=PROJECT_ROOT / settings.publish_media_download_dir,
        )
        cleanup = merge_publish_media_cleanup(cleanup, video_override.cleanup)
        if video_override.path is not None:
            task = replace(
                task,
                job=replace(task.job, video_path=str(video_override.path)),
            )

    return task, client, cleanup


def finish_schedule_publish_cleanup(
    settings,
    cleanup: PublishMediaCleanup | None,
    *,
    airtable=None,
    record_id: str | None = None,
    record_fields: dict | None = None,
) -> None:
    if cleanup is None:
        return
    drive_client = None
    service_account = PROJECT_ROOT / settings.google_sheets_service_account
    if service_account.exists():
        try:
            drive_client = GoogleDriveClient.from_service_account(service_account)
        except GoogleDriveError:
            drive_client = None
    canva_client = None
    if not canva_settings_missing(settings):
        canva_client = canva_client_from_settings(settings)
    apply_publish_media_cleanup(
        cleanup,
        drive=drive_client,
        canva_client=canva_client,
        airtable=airtable,
        record_id=record_id,
        log=print,
    )


def finalize_record_after_platform_publish(
    settings,
    *,
    airtable,
    record_id: str,
    record_fields: dict,
    publish_cleanup: PublishMediaCleanup | None,
    excluded_platforms: frozenset | None = None,
) -> dict:
    fields = dict(record_fields)
    done_record = mark_record_done_and_published_if_complete(
        airtable,
        record_id=record_id,
        record_fields=fields,
        excluded_platforms=excluded_platforms,
    )
    if done_record is not None:
        fields = {**fields, **done_record.fields}
        print(f"Status updated to 6. {STATUS_DONE_PUBLISHED}")
    finish_schedule_publish_cleanup(
        settings,
        publish_cleanup,
        airtable=airtable,
        record_id=record_id,
        record_fields=fields,
    )
    return fields


def load_publish_job_from_airtable(settings, record_id: str):
    client = airtable_client_from_settings(settings)
    record = client.get_record(record_id)
    return record_to_publish_job(record), client


def youtube_client_from_settings(settings) -> YouTubeClient:
    return YouTubeClient(
        client_secrets_path=PROJECT_ROOT / settings.youtube_client_secrets,
        token_path=PROJECT_ROOT / settings.youtube_token,
        expected_channel_handle=settings.youtube_channel_handle,
    )


def happyscribe_download_dir_from_settings(settings) -> Path:
    return PROJECT_ROOT / settings.happyscribe_download_dir


def youtube_settings_missing(settings) -> list[str]:
    missing = []
    if not (PROJECT_ROOT / settings.youtube_client_secrets).exists():
        missing.append(f"client secrets ({settings.youtube_client_secrets})")
    if not (PROJECT_ROOT / settings.youtube_token).exists():
        missing.append(f"token file ({settings.youtube_token})")
    return missing


def attach_local_video_path(job, settings) -> None:
    lookup_name = job.metadata.get(FIELD_TITLE) or job.title
    video_path = find_downloaded_video(
        happyscribe_download_dir_from_settings(settings),
        lookup_name,
    )
    if video_path is None:
        raise YouTubePublishError(
            f"No local video file found for {lookup_name!r} in "
            f"{settings.happyscribe_download_dir}. "
            "Download it first with --download-happyscribe-library."
        )
    job.video_path = str(video_path)


def youtube_settings_complete(settings) -> bool:
    return bool(
        (PROJECT_ROOT / settings.youtube_client_secrets).exists()
        and (PROJECT_ROOT / settings.youtube_token).exists()
    )


def cli_requested_action(args) -> bool:
    return any(
        (
            args.check_config,
            args.test_airtable,
            args.test_happyscribe,
            args.list_happyscribe_library,
            args.download_happyscribe_library,
            args.export_happyscribe_web,
            args.happyscribe_save_session,
            args.happyscribe_import_session,
            args.burn_happyscribe_video,
            args.canva_auth,
            args.canva_auth_code is not None,
            args.test_canva,
            args.canva_download,
            args.canva_resolve,
            args.youtube_auth,
            args.youtube_auth_code is not None,
            args.test_youtube,
            args.test_meta,
            args.resolve_meta,
            args.meta_setup_token is not None,
            args.list_pending,
            args.schedule,
            args.schedule_youtube,
            args.schedule_facebook,
            args.schedule_instagram,
            args.quotes,
            args.watch is not None,
            args.inspect_channel_report,
            args.fix_channel_report_protection,
            args.update_channel_report,
            args.dry_run_channel_report,
            args.channel_report_snapshot,
            args.channel_report_recent_months,
        )
    )


def channel_report_mapping_path(settings) -> Path:
    path = Path(settings.channel_report_mapping)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def google_sheets_client_from_settings(settings) -> GoogleSheetsClient:
    return GoogleSheetsClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )


def youtube_channel_id_from_settings(settings) -> str | None:
    url = settings.youtube_channel_url.strip()
    marker = "/channel/"
    if marker in url:
        channel_id = url.rsplit(marker, 1)[-1].split("/", 1)[0].split("?", 1)[0]
        return channel_id or None
    return None


def channel_report_snapshot_path(settings) -> Path:
    path = Path(settings.channel_report_snapshots)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def run_inspect_channel_report(settings) -> int:
    mapping_path = channel_report_mapping_path(settings)
    try:
        mapping = load_channel_report_mapping(mapping_path)
        sheets = google_sheets_client_from_settings(settings)
        rows = inspect_channel_report_sheet(sheets, mapping)
    except (ChannelReportError, GoogleSheetsError) as exc:
        print(f"Channel report inspect failed: {exc}")
        return 1

    sheet_title = sheets.resolve_sheet_title(
        mapping.spreadsheet_id,
        sheet_gid=mapping.sheet_gid,
        sheet_title=mapping.sheet_title,
    )
    print(f"Spreadsheet: {mapping.spreadsheet_id}")
    print(f"Tab: {sheet_title} (gid={mapping.sheet_gid})")
    print(f"Mapping file: {mapping_path}")
    for index, row in enumerate(rows, start=1):
        print_console("\t".join(row) if row else "")
        if index >= 25:
            break
    return 0


def run_capture_channel_report_snapshots(settings) -> int:
    missing: list[str] = []
    if not settings.meta_access_token:
        missing.append("META_ACCESS_TOKEN")
    if not youtube_settings_complete(settings):
        missing.extend(youtube_settings_missing(settings))
    if missing:
        print("Missing required settings:", ", ".join(dict.fromkeys(missing)))
        return 1

    try:
        youtube_client = youtube_client_from_settings(settings)
        meta_client = meta_client_from_settings(settings)
        page_id, instagram_account_id, _ = resolve_meta_targets(settings)
        result = capture_channel_report_snapshots(
            store_path=channel_report_snapshot_path(settings),
            meta_client=meta_client,
            meta_page_id=page_id or None,
            meta_instagram_account_id=instagram_account_id or None,
            youtube_client=youtube_client,
            youtube_channel_id=youtube_channel_id_from_settings(settings),
        )
    except (ChannelReportSnapshotError, MetaError, YouTubePublishError) as exc:
        print(f"Channel report snapshot failed: {exc}")
        return 1

    print(f"Captured follower snapshots for {result.captured_on.isoformat()}:")
    for platform, metric_key, value in result.recorded:
        print(f"  {platform} {metric_key}: {int(value)}")
    if not result.recorded:
        print("  (no values recorded)")
    return 0


def parse_channel_report_target_month(value: str | None) -> date | None:
    if not value:
        return None
    parsed = parse_month_cell(value.strip())
    if parsed is None:
        raise ChannelReportError(
            f"Invalid --channel-report-month value {value!r}; expected YYYY-MM"
        )
    return date(parsed[0], parsed[1], 1)


def run_update_channel_report(
    settings,
    *,
    dry_run: bool = False,
    all_months: bool = False,
    recent_months: bool = False,
    target_month: date | None = None,
    capture_snapshots: bool = False,
) -> int:
    mapping_path = channel_report_mapping_path(settings)
    missing: list[str] = []
    if not (PROJECT_ROOT / settings.google_sheets_service_account).exists():
        missing.append(f"Google Sheets service account ({settings.google_sheets_service_account})")

    try:
        mapping = load_channel_report_mapping(mapping_path)
    except ChannelReportError as exc:
        print(f"Channel report update failed: {exc}")
        return 1

    platforms = _enabled_platforms(mapping)
    if "youtube" in platforms and not youtube_settings_complete(settings):
        missing.extend(youtube_settings_missing(settings))
    if platforms.intersection({"facebook", "instagram"}) and not settings.meta_access_token:
        missing.append("META_ACCESS_TOKEN")
    if missing and not dry_run:
        print("Missing required settings:", ", ".join(dict.fromkeys(missing)))
        return 1

    try:
        sheets = google_sheets_client_from_settings(settings)
        youtube_client = None
        if youtube_settings_complete(settings):
            youtube_client = youtube_client_from_settings(settings)
        meta_client = None
        page_id = ""
        instagram_account_id = ""
        if settings.meta_access_token:
            page_id, instagram_account_id, _ = resolve_meta_targets(settings)
            meta_client = meta_client_from_settings(settings)
        result = update_channel_report(
            mapping=mapping,
            sheets_client=sheets,
            youtube_client=youtube_client,
            youtube_channel_id=youtube_channel_id_from_settings(settings),
            meta_client=meta_client,
            meta_page_id=page_id or None,
            meta_instagram_account_id=instagram_account_id or None,
            dry_run=dry_run,
            target_month=target_month,
            all_months=all_months,
            recent_months=recent_months,
            snapshot_store_path=channel_report_snapshot_path(settings),
            capture_snapshots=capture_snapshots,
        )
    except (
        ChannelReportError,
        GoogleSheetsError,
        YouTubePublishError,
        MetaError,
    ) as exc:
        print(f"Channel report update failed: {exc}")
        return 1

    updates = result.updates
    label = "Would update" if dry_run else "Updated"
    if not updates:
        print("No channel report cells matched the configured month rows.")
        return 0
    written_count = len(updates) - len(result.skipped_cells)
    print(f"{label} {written_count} cell(s):")
    for item in updates:
        if item.cell in result.skipped_cells:
            continue
        print_console(
            f"{item.cell}\t{item.platform}\t{item.year:04d}-{item.month:02d}\t{item.views}"
        )
    if result.skipped_cells:
        service_account_path = PROJECT_ROOT / settings.google_sheets_service_account
        email_hint = ""
        if service_account_path.is_file():
            try:
                import json as json_module

                payload = json_module.loads(service_account_path.read_text(encoding="utf-8"))
                email = payload.get("client_email")
                if isinstance(email, str) and email:
                    email_hint = f" Service account: {email}."
            except (OSError, ValueError):
                pass
        print(
            f"Skipped {len(result.skipped_cells)} protected cell(s).{email_hint} "
            "In Google Sheets: Data → Protect sheets and ranges → edit the rule → "
            "add the service account email under 'Except specified people who can edit'."
        )
    return 0


def run_fix_channel_report_protection(settings) -> int:
    mapping_path = channel_report_mapping_path(settings)
    if not (PROJECT_ROOT / settings.google_sheets_service_account).exists():
        print(
            "Missing required settings: "
            f"Google Sheets service account ({settings.google_sheets_service_account})"
        )
        return 1
    try:
        mapping = load_channel_report_mapping(mapping_path)
        sheets = google_sheets_client_from_settings(settings)
        merged = ensure_report_write_ranges_unprotected(sheets, mapping)
    except (ChannelReportError, GoogleSheetsError) as exc:
        print(f"Channel report protection fix failed: {exc}")
        return 1

    print(f"Bulgarian tab now has {len(merged)} unprotected write range(s).")
    for item in merged:
        if not isinstance(item, dict):
            continue
        print_console(
            "rows "
            f"{int(item.get('startRowIndex', 0)) + 1}-"
            f"{int(item.get('endRowIndex', 0))} "
            f"cols {item.get('startColumnIndex')}-{item.get('endColumnIndex')}"
        )
    return 0


def build_quotes_pipeline_settings(
    settings,
    *,
    meta_page_id: str,
    meta_instagram_account_id: str,
    publish_mode: str = "staggered",
    private_test: bool = False,
    reference_date=None,
    platforms: tuple[PlatformName, ...] | None = None,
) -> QuotesPipelineSettings:
    return QuotesPipelineSettings(
        work_dir=canva_download_dir_from_settings(settings),
        publish_timezone=settings.quotes_publish_timezone,
        publish_hour=settings.quotes_publish_hour,
        template_urls=template_urls_from_settings(settings),
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
        meta_access_token=settings.meta_access_token or "",
        meta_app_id=settings.meta_app_id,
        youtube_client_secrets=PROJECT_ROOT / settings.youtube_client_secrets,
        youtube_token=PROJECT_ROOT / settings.youtube_token,
        youtube_channel_handle=settings.youtube_channel_handle,
        youtube_playlist_title=settings.youtube_playlist_title,
        youtube_playlist_id=settings.youtube_playlist_id,
        ffmpeg_path=settings.happyscribe_ffmpeg,
        canva_quotes_design_id=settings.canva_quotes_design_id,
        canva_quotes_folder_id=resolve_canva_quotes_folder_id(settings),
        publish_mode=publish_mode,
        private_test=private_test,
        reference_date=reference_date,
        platforms=platforms,
    )


def _env_flag(name: str) -> bool | None:
    value = os.getenv(name, "").strip().lower()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    return None


def resolve_publish_run_mode(
    args,
    *,
    publish_timezone: str,
    publish_hour: int,
) -> tuple[str, "date", bool]:
    """Return publish_mode, reference date, and private_test for a CLI run."""
    from datetime import datetime

    from media_publisher.timezones import get_timezone

    private_test = bool(getattr(args, "private", False))
    now = datetime.now(get_timezone(publish_timezone))
    reference_date = now.date()
    if getattr(args, "schedule", False):
        return "scheduled", reference_date, private_test
    if private_test:
        next_publish_at = next_catalog_publish_at(
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
            now=now,
        )
        reference_date = publish_local_date(next_publish_at, publish_timezone)
        return "scheduled", reference_date, True
    staggered = _env_flag("PUBLISH_STAGGERED")
    if staggered is True:
        return "staggered", reference_date, False
    if staggered is False:
        return "immediate", reference_date, False
    return "staggered", reference_date, False


def print_publish_run_mode(
    *,
    publish_mode: str,
    reference_date,
    private_test: bool,
    content_label: str = "videos",
    publish_timezone: str = "Europe/Sofia",
    publish_hour: int = 18,
) -> None:
    today = reference_date.isoformat()
    tomorrow = (reference_date + timedelta(days=1)).isoformat()
    if publish_mode == "staggered":
        print_console(
            f"Staggered publish for {content_label}: Instagram today ({today}) "
            f"immediately; YouTube/Facebook tomorrow ({tomorrow}) scheduled for review."
        )
        return
    if publish_mode == "scheduled" and not private_test:
        print_console(
            f"Using native platform scheduling for pending {content_label} due on {today}."
        )
        return
    if private_test:
        next_publish_at = next_catalog_publish_at(
            publish_timezone=publish_timezone,
            publish_hour=publish_hour,
        )
        print_console(
            f"Private test for {content_label}: schedule public YouTube and Facebook "
            f"posts due on {today} for {next_publish_at.isoformat()}. "
            "Instagram upload skipped; publish dates and status unchanged."
        )
        return
    print_console(
        f"Publishing pending {content_label} for {today} immediately."
    )


def validate_quotes_pipeline_settings(settings) -> list[str]:
    missing = canva_settings_missing(settings)
    missing.extend(meta_settings_missing(settings))
    missing.extend(youtube_settings_missing(settings))
    return missing


def resolve_selected_platforms(args) -> tuple[PlatformName, ...] | None:
    selected = getattr(args, "platform", None)
    if not selected:
        return None
    return tuple(dict.fromkeys(selected))


def run_quotes_publish(settings, args) -> int:
    missing = validate_quotes_pipeline_settings(settings)
    if missing:
        print("Missing required settings:", ", ".join(dict.fromkeys(missing)))
        return 1

    platforms = resolve_selected_platforms(args)
    publish_mode, reference_date, private_test = resolve_publish_run_mode(
        args,
        publish_timezone=settings.quotes_publish_timezone,
        publish_hour=settings.quotes_publish_hour,
    )
    print_publish_run_mode(
        publish_mode=publish_mode,
        reference_date=reference_date,
        private_test=private_test,
        content_label="quote posts",
        publish_timezone=settings.quotes_publish_timezone,
        publish_hour=settings.quotes_publish_hour,
    )
    if platforms is not None:
        print_console(f"Limiting quote publish to: {', '.join(platforms)}")

    try:
        page_id, instagram_account_id, _ = resolve_meta_targets(settings)
        meta_client = meta_client_from_settings(settings)
        exit_code, _ = run_quotes_pipeline(
            build_quotes_pipeline_settings(
                settings,
                meta_page_id=page_id,
                meta_instagram_account_id=instagram_account_id,
                publish_mode=publish_mode,
                private_test=private_test,
                reference_date=reference_date,
                platforms=platforms,
            ),
            meta_client=meta_client,
            canva_client=canva_client_from_settings(settings),
            print_line=print_console,
        )
    except (MetaError, RuntimeError, QuotePdfError, CanvaError) as exc:
        print(f"Quotes pipeline failed: {exc}")
        return 1
    return exit_code


def build_publish_pipeline_settings(
    settings,
    *,
    meta_page_id: str,
    meta_instagram_account_id: str,
    headless: bool,
    skip_thumbnails: bool = False,
    publish_mode: str = "staggered",
    private_test: bool = False,
    reference_date=None,
    regenerate_videos: bool = False,
    use_web_export: bool = False,
) -> PublishPipelineSettings:
    canva_client = None
    if not skip_thumbnails and not canva_settings_missing(settings):
        canva_client = canva_client_from_settings(settings)

    return PublishPipelineSettings(
        project_root=PROJECT_ROOT,
        publish_timezone=settings.publish_timezone,
        publish_hour=settings.publish_hour,
        canva_download_dir=canva_download_dir_from_settings(settings),
        canva_client=canva_client,
        canva_long_video_thumbnails_url=settings.canva_long_video_thumbnails_url,
        canva_short_video_thumbnails_url=settings.canva_short_video_thumbnails_url,
        skip_thumbnails=skip_thumbnails,
        happyscribe_download_dir=happyscribe_download_dir_from_settings(settings),
        happyscribe_browser_state=happyscribe_browser_state_path(settings),
        happyscribe_browser_profile=happyscribe_browser_profile_path(settings),
        happyscribe_browser_channel=settings.happyscribe_browser_channel,
        happyscribe_api_key=settings.happyscribe_api_key,
        happyscribe_headless=headless,
        ffmpeg_path=settings.happyscribe_ffmpeg,
        youtube_short_cover_intro_seconds=settings.youtube_short_cover_intro_seconds,
        youtube_client_secrets=PROJECT_ROOT / settings.youtube_client_secrets,
        youtube_token=PROJECT_ROOT / settings.youtube_token,
        youtube_channel_handle=settings.youtube_channel_handle,
        youtube_playlist_title=settings.youtube_playlist_title,
        youtube_playlist_id=settings.youtube_playlist_id,
        template_urls=template_urls_from_settings(settings),
        meta_page_id=meta_page_id,
        meta_instagram_account_id=meta_instagram_account_id,
        meta_access_token=settings.meta_access_token or "",
        meta_app_id=settings.meta_app_id,
        publish_mode=publish_mode,
        private_test=private_test,
        reference_date=reference_date,
        regenerate_videos=regenerate_videos,
        use_web_export=use_web_export,
        happyscribe_published_folder_id=settings.happyscribe_published_folder_id,
        publish_override_drive_folder_id=settings.publish_override_drive_folder_id,
        publish_override_thumbnails_subfolder=settings.publish_override_thumbnails_subfolder,
        publish_override_videos_subfolder=settings.publish_override_videos_subfolder,
        canva_published_subfolder_name=settings.canva_published_subfolder_name,
        google_drive_service_account=PROJECT_ROOT / settings.google_sheets_service_account,
        publish_media_download_dir=PROJECT_ROOT / settings.publish_media_download_dir,
        tn_publish_settings=TnPublishSettings(
            original_dir=PROJECT_ROOT / settings.tn_original_thumbnail_dir,
            cache_dir=PROJECT_ROOT / settings.tn_cache_dir,
            output_dir=PROJECT_ROOT / settings.tn_render_output_dir,
            english_override_file=PROJECT_ROOT / settings.tn_english_override_file,
        ),
    )


def validate_publish_pipeline_settings(settings, tasks) -> list[str]:
    missing: list[str] = []
    if not settings.airtable_token:
        missing.append("AIRTABLE_TOKEN")
    if not settings.airtable_base_id:
        missing.append("AIRTABLE_BASE_ID")
    if not settings.airtable_table_name:
        missing.append("AIRTABLE_TABLE_NAME")
    if not tasks:
        return missing
    missing.extend(happyscribe_library_settings_missing(settings))

    platforms = {task.platform for task in tasks}
    if "youtube" in platforms:
        missing.extend(youtube_settings_missing(settings))
    if platforms & {"facebook", "instagram"}:
        missing.extend(meta_settings_missing(settings))
    return missing


def run_default_publish(settings, args) -> int:
    platforms = resolve_selected_platforms(args)
    publish_mode, reference_date, private_test = resolve_publish_run_mode(
        args,
        publish_timezone=settings.publish_timezone,
        publish_hour=settings.publish_hour,
    )
    regenerate_videos = bool(getattr(args, "regenerate_videos", False))
    use_web_export = bool(getattr(args, "happyscribe_web_export", False))
    if regenerate_videos:
        print_console("Re-downloading videos from HappyScribe (ignoring cached local files).")
    print_publish_run_mode(
        publish_mode=publish_mode,
        reference_date=reference_date,
        private_test=private_test,
        publish_timezone=settings.publish_timezone,
        publish_hour=settings.publish_hour,
    )
    if platforms is not None:
        print_console(f"Limiting video publish to: {', '.join(platforms)}")

    try:
        airtable = airtable_client_from_settings(settings)
        schedule = publish_schedule_settings(settings)
        tasks = fetch_pending_schedule_tasks(
            airtable,
            **schedule,
            videos_only=True,
            platforms=platforms,
        )
    except AirtableError as exc:
        print(f"Airtable catalog lookup failed: {exc}")
        return 1

    if not tasks:
        try:
            skipped = fetch_missing_translation_reports(airtable, **schedule)
        except AirtableError as exc:
            print(f"Airtable catalog lookup failed: {exc}")
            return 1
        if skipped:
            print(f"Skipped — missing {FIELD_VIDEO_NAME_TRANSLATED!r} ({len(skipped)}):")
            for report in skipped:
                platforms = ", ".join(report.platforms)
                print_console(f"{report.record_id}\t{report.original_title}\t{platforms}")
        print("No pending schedules found.")
        return 0

    missing = validate_publish_pipeline_settings(settings, tasks)
    if missing:
        print("Missing required settings:", ", ".join(dict.fromkeys(missing)))
        return 1

    meta_client: MetaClient | None = None
    page_id = ""
    instagram_account_id = ""
    if any(task.platform in {"facebook", "instagram"} for task in tasks):
        try:
            page_id, instagram_account_id, _ = resolve_meta_targets(settings)
            meta_client = meta_client_from_settings(settings)
        except MetaError as exc:
            print(f"Meta setup failed: {exc}")
            return 1

    try:
        exit_code, _ = run_publish_pipeline(
            airtable,
            happyscribe_client_from_settings(settings),
            happyscribe_library_from_settings(settings),
            build_publish_pipeline_settings(
                settings,
                meta_page_id=page_id,
                meta_instagram_account_id=instagram_account_id,
                headless=args.happyscribe_export_headless,
                skip_thumbnails=bool(getattr(args, "skip_thumbnails", False)),
                publish_mode=publish_mode,
                private_test=private_test,
                reference_date=reference_date,
                regenerate_videos=regenerate_videos,
                use_web_export=use_web_export,
            ),
            meta_client=meta_client,
            print_line=print_console,
        )
    except (HappyScribeError, AirtableError) as exc:
        print(f"Publish pipeline failed: {exc}")
        return 1
    return exit_code


def run_watch_publish(settings, args) -> int:
    import time

    minutes = args.watch if args.watch and args.watch > 0 else 5
    print_console(
        f"Watching for pending publishes every {minutes} minute(s). Press Ctrl+C to stop."
    )
    while True:
        run_default_publish(settings, args)
        time.sleep(minutes * 60)


def main() -> int:
    configure_console_output()
    settings = load_settings(PROJECT_ROOT)
    parser = build_parser()
    args = parser.parse_args()

    if args.check_config:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        print("Required Airtable settings are present.")
        print("Optional integrations:")
        print(f"  HappyScribe: {'yes' if settings.happyscribe_api_key else 'no'}")
        print(f"  Canva: {'yes' if canva_settings_complete(settings) else 'no'}")
        drive_account = PROJECT_ROOT / settings.google_sheets_service_account
        print(
            "  Google Drive (publish overrides / TN): "
            f"{'yes' if drive_account.is_file() else 'no'}"
        )
        print(f"  YouTube: {'yes' if youtube_settings_complete(settings) else 'no'}")
        if settings.youtube_channel_handle:
            print(f"    Channel: @{settings.youtube_channel_handle}")
        print(f"  Meta: {'yes' if settings.meta_access_token else 'no'}")
        print(f"    Facebook: {meta_facebook_url(settings)}")
        print(f"    Instagram: {meta_instagram_url(settings)}")
        if settings.meta_access_token:
            print(f"    Page ID: {'set' if settings.meta_page_id else 'resolve via username'}")
            print(
                "    Instagram account ID: "
                f"{'set' if settings.meta_instagram_account_id else 'resolve via page link'}"
            )
        return 0

    if args.test_airtable:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = airtable_client_from_settings(settings)
            count = client.test_connection(max_records=1)
        except AirtableError as exc:
            print(f"Airtable connection failed: {exc}")
            return 1
        print(
            f"Airtable connection OK ({settings.airtable_table_name!r}, "
            f"{count} record(s) sampled)."
        )
        return 0

    if args.test_happyscribe:
        missing = happyscribe_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = happyscribe_client_from_settings(settings)
            location = None
            try:
                location = happyscribe_library_from_settings(settings)
            except HappyScribeError:
                location = None
            org_id, count = client.test_connection(location)
        except HappyScribeError as exc:
            print(f"HappyScribe connection failed: {exc}")
            return 1
        if location is not None:
            print(
                "HappyScribe connection OK "
                f"(organization {org_id!r}, folder {location.folder_id!r}, "
                f"{count} transcription(s) sampled)."
            )
        else:
            print(
                f"HappyScribe connection OK (organization {org_id!r}, "
                f"{count} transcription(s) sampled)."
            )
        return 0

    if args.list_happyscribe_library:
        missing = happyscribe_library_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = happyscribe_client_from_settings(settings)
            location = happyscribe_library_from_settings(settings)
            transcriptions = client.list_library_transcriptions(location)
        except HappyScribeError as exc:
            print(f"HappyScribe library listing failed: {exc}")
            return 1
        if not transcriptions:
            print(
                f"No transcriptions found in folder {location.folder_id!r} "
                f"(organization {location.organization_id!r})."
            )
            return 0
        for transcription in transcriptions:
            print_console(
                f"{transcription.id}\t{transcription.state}\t{transcription.name}"
            )
        print_console(f"{len(transcriptions)} transcription(s) in library folder.")
        return 0

    if args.download_happyscribe_library:
        missing = happyscribe_library_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        try:
            client = happyscribe_client_from_settings(settings)
            location = happyscribe_library_from_settings(settings)
            transcriptions = client.list_library_transcriptions(location)
            downloaded = []
            for transcription in transcriptions:
                if transcription.state != TRANSCRIPTION_STATE_READY:
                    continue
                if is_subtitled_export_name(transcription.name):
                    continue
                destination = burned_video_destination_path(
                    download_dir,
                    transcription.name,
                )
                path = client.download_video_with_burned_subtitles(
                    transcription.id,
                    destination,
                    work_dir=download_dir / ".work",
                )
                downloaded.append(path)
                print_console(str(path))
        except HappyScribeError as exc:
            print(f"HappyScribe library download failed: {exc}")
            return 1
        if not downloaded:
            print(
                f"No ready videos to download in folder {location.folder_id!r} "
                f"(organization {location.organization_id!r})."
            )
            return 0
        print(f"Downloaded {len(downloaded)} subtitled video(s) to {download_dir}.")
        return 0

    if args.happyscribe_save_session:
        browser_state = happyscribe_browser_state_path(settings)
        try:
            save_browser_session_interactive(
                browser_state,
                browser_profile_dir=happyscribe_browser_profile_path(settings),
                email=settings.happyscribe_email,
                password=settings.happyscribe_password,
                browser_channel=settings.happyscribe_browser_channel,
            )
        except HappyScribeWebError as exc:
            print(f"HappyScribe session setup failed: {exc}")
            return 1
        print(f"Saved HappyScribe browser session to {settings.happyscribe_browser_state!r}.")
        return 0

    if args.happyscribe_import_session:
        browser_state = happyscribe_browser_state_path(settings)
        try:
            import_browser_session(Path(args.happyscribe_import_session), browser_state)
        except HappyScribeWebError as exc:
            print(f"HappyScribe session import failed: {exc}")
            return 1
        print(f"Imported HappyScribe browser session to {settings.happyscribe_browser_state!r}.")
        return 0

    if args.export_happyscribe_web:
        missing = happyscribe_web_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        try:
            client = happyscribe_client_from_settings(settings)
            transcription = client.get_transcription(args.export_happyscribe_web)
            destination = burned_video_destination_path(download_dir, transcription.name)
            path = export_video_with_subtitles_web(
                args.export_happyscribe_web,
                destination,
                **happyscribe_web_export_kwargs(settings, args),
            )
        except (HappyScribeError, HappyScribeWebError) as exc:
            print(f"HappyScribe web export failed: {exc}")
            return 1
        print(f"Saved web-exported video to {path}")
        print(f"Size bytes: {path.stat().st_size}")
        return 0

    if args.burn_happyscribe_video:
        missing = happyscribe_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.happyscribe_download_dir
        try:
            client = happyscribe_client_from_settings(settings)
            transcription = client.get_transcription(args.burn_happyscribe_video)
            destination = burned_video_destination_path(download_dir, transcription.name)
            path = client.download_video_with_burned_subtitles(
                args.burn_happyscribe_video,
                destination,
                work_dir=download_dir / ".work",
            )
        except HappyScribeError as exc:
            print(f"HappyScribe subtitle burn failed: {exc}")
            return 1
        print(f"Saved subtitled video to {path}")
        print(f"Size bytes: {path.stat().st_size}")
        return 0

    if args.canva_auth:
        missing = []
        if not settings.canva_client_id:
            missing.append("CANVA_CLIENT_ID")
        if not settings.canva_client_secret:
            missing.append("CANVA_CLIENT_SECRET")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            url = client.start_authorization()
        except CanvaError as exc:
            print(f"Canva authorization setup failed: {exc}")
            return 1
        print("Open this URL in a browser and authorize the integration:")
        print(url)
        print()
        print(f"Requested scopes: {' '.join(DEFAULT_SCOPES)}")
        print(
            "If you recently enabled new scopes in the Canva Developer Portal, you "
            "must complete this authorization again so the saved token receives them."
        )
        print()
        print(
            "After approval, run:\n"
            f"  python -m media_publisher --canva-auth-code <authorization_code>"
        )
        return 0

    if args.canva_auth_code:
        missing = []
        if not settings.canva_client_id:
            missing.append("CANVA_CLIENT_ID")
        if not settings.canva_client_secret:
            missing.append("CANVA_CLIENT_SECRET")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            token = client.complete_authorization(
                args.canva_auth_code,
                state=args.canva_auth_state,
            )
        except CanvaError as exc:
            print(f"Canva authorization failed: {exc}")
            return 1
        print(f"Canva token saved to {settings.canva_token!r}.")
        print(f"Granted scopes: {format_access_token_scopes(token.access_token)}")
        missing = missing_canva_scopes(token.access_token)
        if missing:
            print(
                "Warning: Canva integration requires "
                f"{', '.join(missing)}. Re-run --canva-auth after enabling them "
                "in the Canva Developer Portal."
            )
        return 0

    if args.canva_resolve:
        try:
            resolved_url = resolve_canva_url(args.canva_resolve)
            design_id = parse_design_id(resolved_url)
        except CanvaError as exc:
            print(f"Canva URL resolution failed: {exc}")
            return 1
        print(f"Resolved URL: {resolved_url}")
        print(f"Design ID: {design_id}")
        return 0

    if args.canva_download:
        missing = canva_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        download_dir = PROJECT_ROOT / settings.canva_download_dir
        try:
            downloaded = download_images_from_canva_url(
                args.canva_download,
                client_id=settings.canva_client_id or "",
                client_secret=settings.canva_client_secret or "",
                token_path=PROJECT_ROOT / settings.canva_token,
                download_dir=download_dir,
                api_base=settings.canva_api_base,
                redirect_uri=settings.canva_redirect_uri,
                export_format=args.canva_format,
                split_pages=args.canva_split_pages,
            )
        except CanvaError as exc:
            print(f"Canva download failed: {exc}")
            return 1
        for path in downloaded:
            print(path)
        print(f"Downloaded {len(downloaded)} file(s) to {download_dir}.")
        return 0

    if args.test_canva:
        missing = canva_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = canva_client_from_settings(settings)
            token = client.test_connection()
        except CanvaError as exc:
            print(f"Canva connection failed: {exc}")
            return 1
        print("Canva connection OK (token refreshed).")
        print(f"Granted scopes: {format_access_token_scopes(token.access_token)}")
        missing = missing_canva_scopes(token.access_token)
        if missing:
            print(
                "Warning: Canva integration requires "
                f"{', '.join(missing)}. Re-run --canva-auth after enabling them "
                "in the Canva Developer Portal."
            )
        return 0

    if args.youtube_auth:
        secrets_path = PROJECT_ROOT / settings.youtube_client_secrets
        if not secrets_path.exists():
            print(f"Missing required file: {settings.youtube_client_secrets}")
            return 1
        try:
            client = youtube_client_from_settings(settings)
            url = client.start_authorization()
        except YouTubePublishError as exc:
            print(f"YouTube authorization setup failed: {exc}")
            return 1
        print("Open this URL in a browser and authorize the integration:")
        print(url)
        print()
        print(
            f"Sign in with the Google account that manages "
            f"https://www.youtube.com/@{settings.youtube_channel_handle} "
            f"before approving access."
        )
        print()
        print(
            "After approval, copy the authorization code from the redirect URL and run:\n"
            f"  python -m media_publisher --youtube-auth-code <authorization_code>"
        )
        return 0

    if args.youtube_auth_code:
        secrets_path = PROJECT_ROOT / settings.youtube_client_secrets
        if not secrets_path.exists():
            print(f"Missing required file: {settings.youtube_client_secrets}")
            return 1
        try:
            client = youtube_client_from_settings(settings)
            token = client.complete_authorization(
                args.youtube_auth_code,
                state=args.youtube_auth_state,
            )
        except YouTubePublishError as exc:
            print(f"YouTube authorization failed: {exc}")
            return 1
        print(f"YouTube token saved to {settings.youtube_token!r}.")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0

    if args.test_youtube:
        missing = []
        if not (PROJECT_ROOT / settings.youtube_client_secrets).exists():
            missing.append(f"client secrets ({settings.youtube_client_secrets})")
        if not (PROJECT_ROOT / settings.youtube_token).exists():
            missing.append(f"token file ({settings.youtube_token})")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            client = youtube_client_from_settings(settings)
            token = client.test_connection()
            channel = client.verify_authorized_channel()
        except YouTubePublishError as exc:
            print(f"YouTube connection failed: {exc}")
            return 1
        print("YouTube connection OK (token refreshed).")
        print(f"Channel: {channel.title}")
        print(f"URL: {channel.url}")
        if token.scope:
            print(f"Scopes: {token.scope}")
        return 0


    if args.test_meta:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
        except MetaError as exc:
            print(f"Meta connection failed: {exc}")
            return 1
        print("Meta connection OK.")
        print(f"Facebook page: {page_info.name} ({meta_facebook_url(settings)})")
        print(f"Page ID: {page_id}")
        print(f"Instagram: @{page_info.instagram_username or settings.meta_instagram_username}")
        print(f"Instagram URL: {meta_instagram_url(settings)}")
        print(f"Instagram account ID: {instagram_account_id}")
        return 0

    if args.resolve_meta:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
        except MetaError as exc:
            print(f"Meta resolve failed: {exc}")
            return 1
        print("Add these to your .env (optional — usernames are resolved automatically):")
        print(f"META_PAGE_ID={page_id}")
        print(f"META_INSTAGRAM_ACCOUNT_ID={instagram_account_id}")
        print()
        print(f"Facebook: {page_info.name} ({meta_facebook_url(settings)})")
        print(
            "Instagram: "
            f"@{page_info.instagram_username or settings.meta_instagram_username} "
            f"({meta_instagram_url(settings)})"
        )
        return 0

    if args.meta_setup_token is not None:
        missing = []
        if not settings.meta_app_id:
            missing.append("META_APP_ID")
        if not settings.meta_app_secret:
            missing.append("META_APP_SECRET")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1

        user_token = args.meta_setup_token
        if user_token == "__USE_ENV__":
            import os

            user_token = os.getenv("META_USER_ACCESS_TOKEN", "").strip()
            if not user_token:
                print("Provide TOKEN or set META_USER_ACCESS_TOKEN in the environment.")
                return 1

        try:
            credentials = resolve_permanent_page_token(
                user_token,
                page_username=settings.meta_page_username,
                app_id=settings.meta_app_id or "",
                app_secret=settings.meta_app_secret or "",
                api_version=settings.meta_api_version,
            )
            page_token_info = inspect_access_token(
                credentials.access_token,
                app_id=settings.meta_app_id or "",
                app_secret=settings.meta_app_secret or "",
                api_version=settings.meta_api_version,
            )
            env_path = PROJECT_ROOT / ".env"
            updates = {"META_ACCESS_TOKEN": credentials.access_token}
            if credentials.page_id:
                updates["META_PAGE_ID"] = credentials.page_id
            if credentials.instagram_account_id:
                updates["META_INSTAGRAM_ACCOUNT_ID"] = credentials.instagram_account_id
            update_env_values(env_path, updates)
        except MetaError as exc:
            print(f"Meta token setup failed: {exc}")
            return 1

        expiry = (
            "never"
            if page_token_info.expires_at is None
            else page_token_info.expires_at.isoformat()
        )
        print("Permanent Meta Page token saved to .env.")
        print(f"Facebook page: {credentials.name} ({meta_facebook_url(settings)})")
        print(f"Page ID: {credentials.page_id}")
        if credentials.instagram_account_id:
            print(
                "Instagram: "
                f"@{credentials.instagram_username or settings.meta_instagram_username} "
                f"({meta_instagram_url(settings)})"
            )
            print(f"Instagram account ID: {credentials.instagram_account_id}")
        print(f"Token type: {page_token_info.token_type}")
        print(f"Token expires: {expiry}")
        return 0

    if args.list_pending:
        missing = []
        if not settings.airtable_token:
            missing.append("AIRTABLE_TOKEN")
        if not settings.airtable_base_id:
            missing.append("AIRTABLE_BASE_ID")
        if not settings.airtable_table_name:
            missing.append("AIRTABLE_TABLE_NAME")
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        platforms = resolve_selected_platforms(args)
        try:
            client = airtable_client_from_settings(settings)
            schedule = publish_schedule_settings(settings)
            skipped = fetch_missing_translation_reports(client, **schedule)
            tasks = fetch_pending_schedule_tasks(
                client,
                **schedule,
                videos_only=True,
                platforms=platforms,
            )
        except AirtableError as exc:
            print(f"Airtable catalog lookup failed: {exc}")
            return 1
        if skipped:
            print(f"Skipped — missing {FIELD_VIDEO_NAME_TRANSLATED!r} ({len(skipped)}):")
            for report in skipped:
                pending = ", ".join(report.platforms)
                print_console(
                    f"{report.record_id}\t{report.original_title}\t{pending}"
                )
            print()
        if platforms is not None:
            print(f"Platforms: {', '.join(platforms)}")
        if not tasks:
            if skipped:
                print("No videos ready to publish.")
            else:
                print("No pending platform schedules found.")
            return 0
        print(f"Ready to publish ({len(tasks)}):")
        for task in tasks:
            format_label = "short" if task.job.video_format == "short_form" else "video"
            print_console(
                f"{task.record_id}\t{task.platform}\t{format_label}\t"
                f"{task.publish_at.isoformat()}\t{task.job.title}"
            )
        print(f"{len(tasks)} pending schedule(s).")
        return 0

    if args.schedule_youtube:
        missing = youtube_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            task, airtable, publish_cleanup = load_schedule_task(
                settings, args.schedule_youtube, "youtube"
            )
            if not task.job.video_path:
                attach_local_video_path(task.job, settings)
            video_id = publish_to_youtube(
                task.job,
                client_secrets_path=PROJECT_ROOT / settings.youtube_client_secrets,
                token_path=PROJECT_ROOT / settings.youtube_token,
                expected_channel_handle=settings.youtube_channel_handle,
                ffmpeg_path=settings.happyscribe_ffmpeg,
                cover_intro_seconds=settings.youtube_short_cover_intro_seconds,
                playlist_id=settings.youtube_playlist_id,
                playlist_title=settings.youtube_playlist_title,
                **template_urls_from_settings(settings),
            )
            permalink = youtube_video_url(video_id)
            updated = mark_platform_scheduled(
                airtable,
                record_id=task.record_id,
                record_fields=task.record_fields,
                platform="youtube",
                permalink=permalink,
            )
            finalize_record_after_platform_publish(
                settings,
                airtable=airtable,
                record_id=task.record_id,
                record_fields=dict(updated.fields),
                publish_cleanup=publish_cleanup,
            )
        except (AirtableError, YouTubePublishError) as exc:
            print(f"YouTube scheduling failed: {exc}")
            return 1
        when = task.publish_at.isoformat()
        print(
            f"YouTube video scheduled for {when} on @{settings.youtube_channel_handle} "
            f"({permalink})."
        )
        return 0

    if args.schedule_facebook:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, _, page_info = resolve_meta_targets(settings)
            task, airtable, publish_cleanup = load_schedule_task(
                settings, args.schedule_facebook, "facebook"
            )
            if not task.job.video_path:
                attach_local_video_path(task.job, settings)
            meta_client = meta_client_from_settings(settings)
            post_id = publish_to_facebook(
                task.job,
                page_id=page_id,
                access_token=settings.meta_access_token or "",
                app_id=settings.meta_app_id,
                **template_urls_from_settings(settings),
            )
            permalink = meta_client.get_facebook_video_permalink(post_id)
            updated = mark_platform_scheduled(
                airtable,
                record_id=task.record_id,
                record_fields=task.record_fields,
                platform="facebook",
                permalink=permalink,
            )
            finalize_record_after_platform_publish(
                settings,
                airtable=airtable,
                record_id=task.record_id,
                record_fields=dict(updated.fields),
                publish_cleanup=publish_cleanup,
            )
        except (AirtableError, FacebookPublishError, MetaError) as exc:
            print(f"Facebook scheduling failed: {exc}")
            return 1
        when = task.publish_at.isoformat()
        print(
            f"Facebook video scheduled for {when} on {page_info.name} "
            f"({permalink})."
        )
        return 0

    if args.schedule_instagram:
        missing = meta_settings_missing(settings)
        if missing:
            print("Missing required settings:", ", ".join(missing))
            return 1
        try:
            page_id, instagram_account_id, page_info = resolve_meta_targets(settings)
            task, airtable, publish_cleanup = load_schedule_task(
                settings, args.schedule_instagram, "instagram"
            )
            if not task.job.video_path:
                attach_local_video_path(task.job, settings)
            if not instagram_is_due(task.publish_at):
                print(instagram_wait_message(task.publish_at))
                return 0
            duration_seconds = resolve_video_duration_seconds(
                video_path=task.job.video_path,
                metadata=task.job.metadata,
            )
            if instagram_exceeds_api_limit(duration_seconds):
                assert duration_seconds is not None
                print(instagram_duration_skip_message(duration_seconds))
                return 0
            if (
                task.job.video_path
                and not task.job.video_url
                and not settings.meta_app_id
            ):
                print(
                    "Missing META_APP_ID — required when uploading a local video file "
                    "to Instagram."
                )
                return 1
            meta_client = meta_client_from_settings(settings)
            media_id = publish_to_instagram(
                task.job,
                instagram_account_id=instagram_account_id,
                access_token=settings.meta_access_token or "",
                app_id=settings.meta_app_id,
                page_id=page_id,
                ffmpeg_path=settings.happyscribe_ffmpeg,
                **template_urls_from_settings(settings),
            )
            permalink = meta_client.get_instagram_media_permalink(media_id)
            updated = mark_platform_scheduled(
                airtable,
                record_id=task.record_id,
                record_fields=task.record_fields,
                platform="instagram",
                permalink=permalink,
            )
            finalize_record_after_platform_publish(
                settings,
                airtable=airtable,
                record_id=task.record_id,
                record_fields=dict(updated.fields),
                publish_cleanup=publish_cleanup,
            )
        except (AirtableError, InstagramPublishError, MetaError) as exc:
            print(f"Instagram scheduling failed: {exc}")
            return 1
        when = task.publish_at.isoformat()
        ig_handle = page_info.instagram_username or settings.meta_instagram_username
        print(
            f"Instagram Reel published for {when} on @{ig_handle} "
            f"({permalink})."
        )
        return 0

    if args.watch is not None:
        try:
            return run_watch_publish(settings, args)
        except KeyboardInterrupt:
            print_console("Stopped.")
            return 0

    if args.fix_channel_report_protection:
        return run_fix_channel_report_protection(settings)

    if args.inspect_channel_report:
        return run_inspect_channel_report(settings)

    if args.channel_report_snapshot:
        return run_capture_channel_report_snapshots(settings)

    if args.update_channel_report or args.dry_run_channel_report:
        try:
            target_month = parse_channel_report_target_month(args.channel_report_month)
        except ChannelReportError as exc:
            print(str(exc))
            return 1
        return run_update_channel_report(
            settings,
            dry_run=args.dry_run_channel_report,
            all_months=args.channel_report_all_months,
            recent_months=args.channel_report_recent_months,
            target_month=target_month,
            capture_snapshots=not args.dry_run_channel_report,
        )

    if args.quotes:
        return run_quotes_publish(settings, args)

    if not cli_requested_action(args):
        return run_default_publish(settings, args)

    parser.error(
        "No action specified. Try --check-config, --test-airtable, --list-pending, "
        "--test-happyscribe, --list-happyscribe-library, --download-happyscribe-library, "
        "--happyscribe-save-session, --happyscribe-import-session, --export-happyscribe-web, --burn-happyscribe-video, "
        "--canva-auth, --canva-download, --canva-resolve, --test-canva, --youtube-auth, --test-youtube, "
        "--test-meta, --resolve-meta, --meta-setup-token, --schedule-youtube, "
        "--schedule-facebook, --schedule-instagram, --quotes, --schedule, --watch, "
        "--inspect-channel-report, or --update-channel-report"
    )
    return 2


if __name__ == "__main__":
    exit_code = main()
    try:
        message = maybe_persist_canva_token(PROJECT_ROOT)
        if message:
            print_console(message)
    except RuntimeError as exc:
        print_console(f"Warning: {exc}")
    sys.exit(exit_code)
