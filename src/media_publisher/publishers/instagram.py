from __future__ import annotations

from pathlib import Path

from media_publisher.models import PublishJob
from media_publisher.publishers.meta import MetaClient, MetaError


class InstagramPublishError(RuntimeError):
    pass


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def _build_caption(job: PublishJob) -> str:
    if job.description:
        return job.description
    return job.title


def publish_to_instagram(
    job: PublishJob,
    *,
    instagram_account_id: str,
    access_token: str,
    app_id: str | None = None,
) -> str:
    """Publish or schedule an Instagram Reel and return the media ID."""
    video_path, video_url = _resolve_video(job)
    if not video_path and not video_url:
        raise InstagramPublishError("PublishJob is missing video_path and video_url")

    try:
        client = MetaClient(access_token, app_id=app_id)
        return client.schedule_instagram_reel(
            instagram_account_id=instagram_account_id,
            caption=_build_caption(job),
            video_path=video_path,
            video_url=video_url,
            publish_at=job.publish_at,
        )
    except MetaError as exc:
        raise InstagramPublishError(str(exc)) from exc
