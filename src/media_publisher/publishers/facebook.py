from __future__ import annotations

from pathlib import Path

from media_publisher.models import PublishJob
from media_publisher.publishers.meta import MetaClient, MetaError


class FacebookPublishError(RuntimeError):
    pass


def _resolve_video(job: PublishJob) -> tuple[Path | None, str | None]:
    if job.video_path:
        return Path(job.video_path), None
    if job.video_url:
        return None, job.video_url
    return None, None


def publish_to_facebook(job: PublishJob, *, page_id: str, access_token: str) -> str:
    """Publish or schedule a video on a Facebook Page and return the video ID."""
    video_path, video_url = _resolve_video(job)
    if not video_path and not video_url:
        raise FacebookPublishError("PublishJob is missing video_path and video_url")

    try:
        client = MetaClient(access_token)
        return client.schedule_facebook_video(
            page_id=page_id,
            title=job.title,
            description=job.description,
            video_path=video_path,
            video_url=video_url,
            publish_at=job.publish_at,
        )
    except MetaError as exc:
        raise FacebookPublishError(str(exc)) from exc
