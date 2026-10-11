from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.video_duration import (
    ensure_instagram_upload_video,
    InstagramVideoPrepError,
)


class InstagramPublishError(RuntimeError):
    pass


def publish_instagram_playlist_comments(
    client: MetaClient,
    media_id: str,
    record_fields: dict[str, Any],
    *,
    log: Callable[[str], None] = print,
) -> None:
    """Post configured playlist comments. A comment failure does not unpublish the video."""
    from catalog_parser.playlists import instagram_comments_for_playlists
    from media_publisher.sources.airtable import FIELD_PLAYLISTS, _field_multi_select

    names = _field_multi_select(record_fields.get(FIELD_PLAYLISTS))
    for message in instagram_comments_for_playlists(names):
        try:
            comment_id = client.create_instagram_comment(
                media_id=media_id,
                message=message,
            )
        except MetaError as exc:
            log(f"Instagram comment failed: {exc}")
            return
        log(f"Instagram comment posted: {comment_id}")


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def _build_caption(job: PublishJob) -> str:
    return job.description.strip() or job.title


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
