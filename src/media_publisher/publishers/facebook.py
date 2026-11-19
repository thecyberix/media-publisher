from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from media_publisher.models import PublishJob
from media_publisher.post_templates import prepare_publish_job
from media_publisher.publishers.meta import MetaClient, MetaError


class FacebookPublishError(RuntimeError):
    pass


def remember_facebook_playlist_post(
    post_id: str,
    record_fields: dict[str, Any],
    *,
    permalink: str = "",
    title: str = "",
    state_path: Path,
    log: Callable[[str], None] = print,
) -> None:
    """Add a video that needs a playlist prompt to the Facebook watch list.

    Grok Bot types that prompt when it resubmits the scheduled post. A video
    with no prompt is left off the list.
    """
    from catalog_parser.playlists import instagram_comments_for_playlists, load_playlist_config
    from media_publisher.facebook_comment_responses import remember_facebook_prompt_post
    from media_publisher.sources.airtable import FIELD_PLAYLISTS, _field_multi_select

    names = _field_multi_select(record_fields.get(FIELD_PLAYLISTS))
    config = load_playlist_config()
    messages = instagram_comments_for_playlists(names, config=config)
    end_cards = [
        rule.end_card
        for rule in config.rules
        if rule.end_card and rule.comment in messages and rule.name in names
    ]
    if not messages or not end_cards:
        return
    remember_facebook_prompt_post(
        state_path,
        post_id=post_id,
        end_cards=end_cards,
        comments=messages,
        permalink=permalink,
        title=title,
    )
    log(
        "Facebook watch list: "
        f"{post_id.strip()} end_cards={','.join(end_cards)}"
    )


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
