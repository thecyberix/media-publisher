from __future__ import annotations

from pathlib import Path

from media_publisher.events.templates import (
    EVENT_TYPE_SURYA_KRIYA,
    RenderedEvent,
    get_program,
)
from media_publisher.publishers.meta import MetaClient, MetaError


def default_facebook_image_path(
    project_root: Path,
    *,
    event_type: str = EVENT_TYPE_SURYA_KRIYA,
) -> Path:
    program = get_program(event_type)
    return (project_root / program.facebook_image).resolve()


def publish_event_to_facebook(
    client: MetaClient,
    *,
    page_id: str,
    rendered: RenderedEvent,
    image_path: Path | None = None,
) -> tuple[str, str]:
    """Post the event photo with the program caption text.

    Returns ``(post_id, permalink)``.
    """
    resolved = image_path
    if resolved is None:
        resolved = (Path.cwd() / rendered.facebook_image).resolve()
    if not resolved.is_file():
        raise MetaError(f"Facebook event image not found: {resolved}")

    post_id = client.create_facebook_photo_post(
        page_id=page_id,
        message=rendered.facebook_post_text,
        image_path=resolved,
    )
    permalink = client.get_facebook_post_permalink(post_id)
    return post_id, permalink
