from __future__ import annotations

from media_publisher.models import PublishJob


class FacebookPublishError(RuntimeError):
    pass


def publish_to_facebook(job: PublishJob, *, page_id: str, access_token: str) -> str:
    """Publish a video to a Facebook Page and return the post ID."""
    raise NotImplementedError("Facebook publishing is not implemented yet")
