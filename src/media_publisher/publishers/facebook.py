from __future__ import annotations

from pathlib import Path

from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.meta import MetaClient, MetaError


class FacebookPublishError(RuntimeError):
    pass


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def publish_to_facebook(
    job: PublishJob,
    *,
    page_id: str,
    access_token: str,
    app_id: str | None = None,
    facebook_url: str | None = None,
    instagram_url: str | None = None,
    youtube_channel_url: str | None = None,
) -> str:
    """Publish or schedule a video on a Facebook Page and return the video ID."""
    from media_publisher.post_templates import (
        DEFAULT_FACEBOOK_PAGE_URL,
        DEFAULT_INSTAGRAM_PROFILE_URL,
        DEFAULT_YOUTUBE_CHANNEL_URL,
    )

    job = prepare_publish_job(
        job,
        "facebook",
        facebook_url=facebook_url or DEFAULT_FACEBOOK_PAGE_URL,
        instagram_url=instagram_url or DEFAULT_INSTAGRAM_PROFILE_URL,
        youtube_channel_url=youtube_channel_url or DEFAULT_YOUTUBE_CHANNEL_URL,
    )
    video_path, video_url = _resolve_video(job)
    if not video_path and not video_url:
        raise FacebookPublishError("PublishJob is missing video_path and video_url")

    thumbnail_path = Path(job.thumbnail_path) if job.thumbnail_path else None

    try:
        client = MetaClient(access_token, app_id=app_id)
        # Facebook has no YouTube-style "private until schedule" status.
        # A future publish_at always means public when it goes live (SCHEDULED);
        # immediate publish is public (PUBLISHED). Never map privacy_status to DRAFT.
        if job.video_format == "short_form":
            return client.schedule_facebook_reel(
                page_id=page_id,
                title=job.title,
                description=job.description,
                video_path=video_path,
                video_url=video_url,
                publish_at=job.publish_at,
                unpublished=False,
                thumbnail_path=thumbnail_path,
            )
        return client.schedule_facebook_video(
            page_id=page_id,
            title=job.title,
            description=job.description,
            video_path=video_path,
            video_url=video_url,
            publish_at=job.publish_at,
            unpublished=False,
            thumbnail_path=thumbnail_path,
        )
    except MetaError as exc:
        raise FacebookPublishError(str(exc)) from exc
