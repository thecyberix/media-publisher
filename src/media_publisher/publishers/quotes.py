from __future__ import annotations

from datetime import datetime
from pathlib import Path

from media_publisher.models import PlatformName, PublishJob
from media_publisher.post_templates import (
    build_quote_post_caption,
    build_quote_youtube_description,
    build_quote_youtube_title,
)
from media_publisher.publishers.facebook import FacebookPublishError
from media_publisher.publishers.instagram import InstagramPublishError
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.publishers.youtube import (
    DEFAULT_YOUTUBE_PLAYLIST_TITLE,
    YouTubePublishError,
    publish_to_youtube,
    youtube_video_url,
)
from media_publisher.sources.image_video import ImageVideoError, ensure_quote_video


def publish_local_quote_to_facebook(
    *,
    image_path: Path,
    caption: str,
    publish_at: datetime | None,
    page_id: str,
    access_token: str,
    unpublished: bool = False,
) -> str:
    try:
        client = MetaClient(access_token)
        return client.schedule_facebook_photo(
            page_id=page_id,
            caption=caption,
            image_path=image_path,
            publish_at=publish_at,
            unpublished=unpublished,
        )
    except MetaError as exc:
        raise FacebookPublishError(str(exc)) from exc


def publish_local_quote_to_instagram(
    *,
    image_path: Path,
    caption: str,
    publish_at: datetime | None,
    page_id: str,
    instagram_account_id: str,
    access_token: str,
    app_id: str | None = None,
) -> str:
    try:
        client = MetaClient(access_token, app_id=app_id)
        return client.schedule_instagram_image(
            instagram_account_id=instagram_account_id,
            caption=caption,
            image_path=image_path,
            page_id=page_id,
            publish_at=publish_at,
        )
    except MetaError as exc:
        raise InstagramPublishError(str(exc)) from exc


def publish_local_quote_to_youtube(
    *,
    image_path: Path,
    caption: str,
    publish_at: datetime | None,
    client_secrets_path: Path,
    token_path: Path,
    expected_channel_handle: str | None,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    template_urls: dict[str, str] | None = None,
    playlist_id: str | None = None,
    playlist_title: str | None = None,
    daily_playlist_id: str | None = None,
    daily_playlist_title: str | None = None,
    daily_playlist_slots_path: Path | None = None,
    private: bool = False,
) -> str:
    try:
        video_path = ensure_quote_video(
            image_path,
            work_dir,
            ffmpeg_path=ffmpeg_path,
        )
    except ImageVideoError as exc:
        raise YouTubePublishError(str(exc)) from exc

    title = build_quote_youtube_title(caption)
    job = PublishJob(
        title=title,
        description=build_quote_youtube_description(caption),
        video_path=str(video_path),
        thumbnail_path=str(image_path),
        publish_at=publish_at,
        video_format="short_form",
        content_kind="image",
    )
    if publish_at is None:
        from media_publisher.scheduling import prepare_job_for_immediate_publish

        prepare_job_for_immediate_publish(job, private=private)
    video_id = publish_to_youtube(
        job,
        client_secrets_path=client_secrets_path,
        token_path=token_path,
        expected_channel_handle=expected_channel_handle,
        ffmpeg_path=ffmpeg_path,
        playlist_id=playlist_id,
        playlist_title=playlist_title or DEFAULT_YOUTUBE_PLAYLIST_TITLE,
        daily_playlist_id=daily_playlist_id,
        daily_playlist_title=daily_playlist_title,
        daily_playlist_slots_path=daily_playlist_slots_path,
        **(template_urls or {}),
    )
    return youtube_video_url(video_id)


def publish_local_quote(
    *,
    image_path: Path,
    caption: str,
    publish_at: datetime | None,
    platform: PlatformName,
    page_id: str,
    instagram_account_id: str,
    access_token: str,
    meta_client: MetaClient,
    meta_app_id: str | None = None,
    youtube_client_secrets: Path | None = None,
    youtube_token: Path | None = None,
    youtube_channel_handle: str | None = None,
    youtube_work_dir: Path | None = None,
    youtube_playlist_id: str | None = None,
    youtube_playlist_title: str | None = None,
    youtube_daily_playlist_id: str | None = None,
    youtube_daily_playlist_title: str | None = None,
    youtube_daily_playlist_slots_path: Path | None = None,
    ffmpeg_path: str | None = None,
    template_urls: dict[str, str] | None = None,
    private: bool = False,
) -> str:
    post_caption = build_quote_post_caption(caption)
    if platform == "youtube":
        if not youtube_client_secrets or not youtube_token or not youtube_work_dir:
            raise YouTubePublishError("YouTube quote publishing is not configured")
        return publish_local_quote_to_youtube(
            image_path=image_path,
            caption=caption,
            publish_at=publish_at,
            client_secrets_path=youtube_client_secrets,
            token_path=youtube_token,
            expected_channel_handle=youtube_channel_handle,
            work_dir=youtube_work_dir,
            ffmpeg_path=ffmpeg_path,
            template_urls=template_urls,
            playlist_id=youtube_playlist_id,
            playlist_title=youtube_playlist_title,
            daily_playlist_id=youtube_daily_playlist_id,
            daily_playlist_title=youtube_daily_playlist_title,
            daily_playlist_slots_path=youtube_daily_playlist_slots_path,
            private=private,
        )

    if platform == "facebook":
        photo_id = publish_local_quote_to_facebook(
            image_path=image_path,
            caption=post_caption,
            publish_at=publish_at,
            page_id=page_id,
            access_token=access_token,
            # Schedule for later = public at publish_at; immediate = public now.
            unpublished=False,
        )
        if publish_at is None and private:
            return meta_client.get_facebook_post_permalink(photo_id)
        return meta_client.get_facebook_photo_permalink(photo_id)

    if platform == "instagram":
        media_id = publish_local_quote_to_instagram(
            image_path=image_path,
            caption=post_caption,
            publish_at=publish_at,
            page_id=page_id,
            instagram_account_id=instagram_account_id,
            access_token=access_token,
            app_id=meta_app_id,
        )
        return meta_client.get_instagram_media_permalink(media_id)

    raise ValueError(f"Unsupported platform {platform!r}")


def _require_image_path(job: PublishJob) -> Path:
    if not job.image_path:
        raise ValueError("PublishJob is missing image_path")
    path = Path(job.image_path)
    if not path.is_file():
        raise ValueError(f"Quote image file not found: {path}")
    return path
