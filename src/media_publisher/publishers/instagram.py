from __future__ import annotations

from media_publisher.models import PublishJob


class InstagramPublishError(RuntimeError):
    pass


def publish_to_instagram(
    job: PublishJob,
    *,
    instagram_account_id: str,
    access_token: str,
) -> str:
    """Publish a video to Instagram and return the media ID."""
    raise NotImplementedError("Instagram publishing is not implemented yet")
