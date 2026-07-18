from __future__ import annotations

from pathlib import Path

from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.video_duration import (
    ensure_instagram_upload_video,
    InstagramVideoPrepError,
)


class InstagramPublishError(RuntimeError):
    pass


INSTAGRAM_VIDEO_TYPE_SKIP_MESSAGE = (
    "instagram: skipped — catalog Type is Video "
    "(Instagram publishes Reels/Shorts only)"
)


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def _build_caption(job: PublishJob) -> str:
    return job.description.strip() or job.title


def instagram_skips_video_type(job: PublishJob) -> bool:
    """True when this catalog job should not publish to Instagram (Type=Video)."""
    return job.video_format == "post"


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
    if instagram_skips_video_type(job):
        raise InstagramPublishError(INSTAGRAM_VIDEO_TYPE_SKIP_MESSAGE)

    video_path, video_url = _resolve_video(job)
    if not video_path and not video_url:
        raise InstagramPublishError("PublishJob is missing video_path and video_url")

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
