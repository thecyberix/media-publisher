from __future__ import annotations

from pathlib import Path

from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError
from media_publisher.video_duration import (
    INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES,
    ensure_instagram_upload_video,
    instagram_duration_skip_message,
    instagram_exceeds_api_limit,
    InstagramVideoPrepError,
    resolve_video_duration_seconds,
)


class InstagramPublishError(RuntimeError):
    pass


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def _build_caption(job: PublishJob) -> str:
    return job.description.strip() or job.title


def _should_host_on_drive(video_path: Path) -> bool:
    try:
        return video_path.stat().st_size > INSTAGRAM_SINGLE_UPLOAD_MAX_BYTES
    except OSError:
        return False


def publish_to_instagram(
    job: PublishJob,
    *,
    instagram_account_id: str,
    access_token: str,
    app_id: str | None = None,
    page_id: str | None = None,
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    youtube_channel_url: str | None = None,
    ffmpeg_path: str | None = None,
    drive_client: GoogleDriveClient | None = None,
    drive_host_folder_id: str | None = None,
) -> str:
    """Publish or schedule an Instagram video and return the media ID."""
    from media_publisher.post_templates import (
        DEFAULT_FACEBOOK_PAGE_URL,
        DEFAULT_INSTAGRAM_PROFILE_URL,
        DEFAULT_YOUTUBE_CHANNEL_URL,
    )

    job = prepare_publish_job(
        job,
        "instagram",
        facebook_url=facebook_url or DEFAULT_FACEBOOK_PAGE_URL,
        instagram_url=instagram_url or DEFAULT_INSTAGRAM_PROFILE_URL,
        youtube_channel_url=youtube_channel_url or DEFAULT_YOUTUBE_CHANNEL_URL,
    )
    video_path, video_url = _resolve_video(job)
    if not video_path and not video_url:
        raise InstagramPublishError("PublishJob is missing video_path and video_url")

    duration_seconds = resolve_video_duration_seconds(
        video_path=video_path,
        metadata=job.metadata,
    )
    if instagram_exceeds_api_limit(duration_seconds):
        assert duration_seconds is not None
        raise InstagramPublishError(instagram_duration_skip_message(duration_seconds))

    caption = _build_caption(job)
    cover_path = Path(job.thumbnail_path) if job.thumbnail_path else None

    try:
        client = MetaClient(access_token, app_id=app_id)
        if video_path is not None:
            try:
                video_path = ensure_instagram_upload_video(
                    video_path,
                    ffmpeg_path=ffmpeg_path,
                )
            except InstagramVideoPrepError as exc:
                raise InstagramPublishError(str(exc)) from exc

            # Large local files often fail IG rupload (ProcessingFailedError) and
            # Facebook CDN hosting (2207076). Host temporarily on Drive instead.
            if (
                drive_client is not None
                and drive_host_folder_id
                and _should_host_on_drive(video_path)
            ):
                try:
                    hosted = drive_client.host_public_video(
                        drive_host_folder_id,
                        video_path,
                    )
                except GoogleDriveError as exc:
                    raise InstagramPublishError(
                        f"Failed to host Instagram video on Drive: {exc}"
                    ) from exc
                media_id = client.schedule_instagram_reel(
                    instagram_account_id=instagram_account_id,
                    caption=caption,
                    video_url=hosted.url,
                    page_id=page_id,
                    publish_at=None,
                    cover_path=cover_path,
                )
                try:
                    drive_client.remove_file(hosted.file_id)
                except GoogleDriveError:
                    # Publish already succeeded; orphaned temp file is non-fatal.
                    pass
                return media_id

            return client.schedule_instagram_reel(
                instagram_account_id=instagram_account_id,
                caption=caption,
                video_path=video_path,
                video_url=video_url,
                page_id=page_id,
                publish_at=None,
                cover_path=cover_path,
                prefer_resumable_upload=True,
            )
        if not video_url:
            raise InstagramPublishError(
                "Instagram posts require a local video_path or a public video_url"
            )
        return client.schedule_instagram_feed_video(
            instagram_account_id=instagram_account_id,
            caption=caption,
            video_url=video_url,
            publish_at=None,
        )
    except MetaError as exc:
        raise InstagramPublishError(str(exc)) from exc
