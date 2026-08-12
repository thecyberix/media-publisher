from __future__ import annotations

from pathlib import Path

from media_publisher.events.templates import RenderedEvent
from media_publisher.publishers.meta import MetaClient, MetaError

# Default image for Surya Kriya Facebook photo posts (16:9, cropped from left).
DEFAULT_EVENT_FACEBOOK_IMAGE = Path("events") / "assets" / "surya-kriya-fb.jpg"


def default_facebook_image_path(project_root: Path) -> Path:
    return (project_root / DEFAULT_EVENT_FACEBOOK_IMAGE).resolve()


def publish_event_to_facebook(
    client: MetaClient,
    *,
    page_id: str,
    rendered: RenderedEvent,
    image_path: Path | None = None,
) -> tuple[str, str]:
    """Post the event photo with the same caption text as the public page.

    Returns ``(post_id, permalink)``.
    """
    if image_path is None:
        raise MetaError("Facebook event image_path is required")
    if not image_path.is_file():
        raise MetaError(f"Facebook event image not found: {image_path}")

    post_id = client.create_facebook_photo_post(
        page_id=page_id,
        message=rendered.facebook_post_text,
        image_path=image_path,
    )
    permalink = client.get_facebook_post_permalink(post_id)
    return post_id, permalink
