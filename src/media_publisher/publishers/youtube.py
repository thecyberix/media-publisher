from __future__ import annotations

from media_publisher.models import PublishJob


class YouTubePublishError(RuntimeError):
    pass


def publish_to_youtube(job: PublishJob) -> str:
    """Upload a video to YouTube and return the published video ID."""
    raise NotImplementedError("YouTube publishing is not implemented yet")
