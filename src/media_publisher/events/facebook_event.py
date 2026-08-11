from __future__ import annotations

from media_publisher.events.templates import RenderedEvent
from media_publisher.publishers.meta import MetaClient, MetaError


def publish_event_to_facebook(
    client: MetaClient,
    *,
    page_id: str,
    rendered: RenderedEvent,
) -> tuple[str, str, str]:
    """Post the event text, then comment with the registration link.

    Returns ``(post_id, comment_id, permalink)``.
    """
    post_id = client.create_facebook_feed_post(
        page_id=page_id,
        message=rendered.facebook_post_text,
    )
    try:
        comment_id = client.create_facebook_comment(
            object_id=post_id,
            message=rendered.facebook_comment_text,
        )
    except MetaError as exc:
        raise MetaError(
            f"Facebook event post created ({post_id}) but commenting failed: {exc}. "
            "Ensure the page token includes pages_manage_engagement."
        ) from exc
    permalink = client.get_facebook_post_permalink(post_id)
    return post_id, comment_id, permalink
