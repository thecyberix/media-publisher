from __future__ import annotations

from pathlib import Path

from media_publisher.events.templates import (
    EVENT_IMAGES_DRIVE_FOLDER_ID,
    EVENT_TYPE_SURYA_KRIYA,
    RenderedEvent,
    get_program,
)
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.sources.google_drive import GoogleDriveClient, GoogleDriveError


class EventImageError(RuntimeError):
    pass


def event_image_cache_dir(project_root: Path) -> Path:
    return project_root / "downloads" / "event-images"


def resolve_facebook_image_from_drive(
    *,
    project_root: Path,
    event_type: str = EVENT_TYPE_SURYA_KRIYA,
    drive_client: GoogleDriveClient,
    folder_id: str = EVENT_IMAGES_DRIVE_FOLDER_ID,
) -> Path:
    """Download the programme Facebook image from Drive into a local cache."""
    program = get_program(event_type)
    cache_dir = event_image_cache_dir(project_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / program.facebook_image_name

    try:
        item = drive_client.find_child_by_name(folder_id, program.facebook_image_name)
    except GoogleDriveError as exc:
        raise EventImageError(f"Failed to list event images in Drive: {exc}") from exc
    if item is None:
        raise EventImageError(
            f"Event image {program.facebook_image_name!r} not found in Drive folder "
            f"{folder_id}"
        )
    if not item.mime_type.startswith("image/"):
        raise EventImageError(
            f"Drive file {program.facebook_image_name!r} is not an image "
            f"(mime={item.mime_type!r})"
        )
    try:
        return drive_client.download_file(item.id, destination)
    except GoogleDriveError as exc:
        raise EventImageError(
            f"Failed to download {program.facebook_image_name!r} from Drive: {exc}"
        ) from exc


def publish_event_to_facebook(
    client: MetaClient,
    *,
    page_id: str,
    rendered: RenderedEvent,
    image_path: Path,
) -> tuple[str, str]:
    """Post the event photo with the program caption text.

    Returns ``(post_id, permalink)``.
    """
    resolved = image_path.resolve()
    if not resolved.is_file():
        raise MetaError(f"Facebook event image not found: {resolved}")

    post_id = client.create_facebook_photo_post(
        page_id=page_id,
        message=rendered.facebook_post_text,
        image_path=resolved,
    )
    permalink = client.get_facebook_post_permalink(post_id)
    return post_id, permalink
