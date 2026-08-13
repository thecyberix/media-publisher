from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_publisher.events.page import load_events
from media_publisher.events.templates import (
    EVENT_IMAGES_DRIVE_FOLDER_ID,
    EVENT_TYPE_SURYA_KRIYA,
    RenderedEvent,
    get_program,
    normalize_event_type,
)
from media_publisher.publishers.meta import MetaClient, MetaError
from media_publisher.sources.google_drive import (
    FOLDER_MIME_TYPE,
    DriveFile,
    GoogleDriveClient,
    GoogleDriveError,
)

IMAGE_ROTATION_RELATIVE = Path("data") / "facebook-image-rotation.json"
_SAFE_FILENAME_RE = re.compile(r"[^\w.\-]+", re.UNICODE)


class EventImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelectedEventImage:
    drive_file: DriveFile
    local_path: Path
    selection: str  # "explicit" | "rotation"


def event_image_cache_dir(project_root: Path) -> Path:
    return project_root / "downloads" / "event-images"


def image_rotation_path(events_root: Path) -> Path:
    return events_root / IMAGE_ROTATION_RELATIVE


def load_image_rotation_state(events_root: Path) -> dict[str, list[str]]:
    """Load append-only image usage history per event type."""
    path = image_rotation_path(events_root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    state: dict[str, list[str]] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        # Support both {"surya_kriya": ["id", ...]} and
        # {"surya_kriya": {"history": ["id", ...]}}.
        if isinstance(value, dict):
            value = value.get("history")
        if not isinstance(value, list):
            continue
        ids = [item for item in value if isinstance(item, str) and item.strip()]
        state[key] = ids
    return state


def save_image_rotation_state(events_root: Path, state: dict[str, list[str]]) -> Path:
    path = image_rotation_path(events_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def usage_history_from_events(
    events: list[dict[str, Any]],
    *,
    event_type: str,
) -> list[str]:
    """Return facebook image ids used by prior events of this type (oldest first)."""
    normalized = normalize_event_type(event_type)
    rows: list[tuple[str, str]] = []
    for item in events:
        if normalize_event_type(str(item.get("event_type") or "")) != normalized:
            continue
        image_id = str(item.get("facebook_image_id") or "").strip()
        if not image_id:
            continue
        created_at = str(item.get("created_at") or "")
        rows.append((created_at, image_id))
    rows.sort(key=lambda row: row[0])
    return [image_id for _created, image_id in rows]


def merge_usage_history(rotation_history: list[str], event_history: list[str]) -> list[str]:
    """Prefer durable rotation history; seed from events when rotation is empty.

    If events contain a longer continuation of the same sequence (rotation was not
    saved for the latest publish), keep the longer event history.
    """
    if not rotation_history:
        return list(event_history)
    if not event_history:
        return list(rotation_history)
    if (
        len(event_history) > len(rotation_history)
        and event_history[: len(rotation_history)] == rotation_history
    ):
        return list(event_history)
    return list(rotation_history)


def used_ids_in_current_cycle(
    history: list[str],
    *,
    image_ids: set[str],
) -> list[str]:
    """Replay usage history and return unused-cycle membership for the next pick.

    Explicit and automatic selections both append to ``history``. Once every image
    in ``image_ids`` has been used in the current cycle, the next pick starts a
    fresh cycle.
    """
    if not image_ids:
        return []
    used: list[str] = []
    for image_id in history:
        if image_id not in image_ids:
            continue
        if image_ids <= set(used):
            used = []
        if image_id not in used:
            used.append(image_id)
    if image_ids <= set(used):
        return []
    return used


def list_programme_images(
    drive_client: GoogleDriveClient,
    *,
    event_type: str,
    folder_id: str = EVENT_IMAGES_DRIVE_FOLDER_ID,
) -> list[DriveFile]:
    program = get_program(event_type)
    try:
        subfolder = drive_client.find_child_folder(folder_id, program.facebook_image_folder)
    except GoogleDriveError as exc:
        raise EventImageError(
            f"Failed to list programme folders under Drive root {folder_id}: {exc}"
        ) from exc
    if subfolder is None or subfolder.mime_type != FOLDER_MIME_TYPE:
        raise EventImageError(
            f"Programme image folder {program.facebook_image_folder!r} not found under "
            f"Drive root {folder_id}"
        )
    try:
        children = drive_client.list_children(subfolder.id)
    except GoogleDriveError as exc:
        raise EventImageError(
            f"Failed to list images in {program.facebook_image_folder!r}: {exc}"
        ) from exc
    images = [item for item in children if item.mime_type.startswith("image/")]
    images.sort(key=lambda item: (item.name.casefold(), item.id))
    if not images:
        raise EventImageError(
            f"No images found in Drive folder {program.facebook_image_folder!r}"
        )
    return images


def resolve_image_selector(
    images: list[DriveFile],
    *,
    selector: str,
    folder_name: str,
) -> DriveFile:
    """Resolve an image by Drive file id, filename, or basename (e.g. ``1`` → ``1.jpg``)."""
    requested = selector.strip()
    if not requested:
        raise EventImageError("Image selector is empty")

    by_id = {item.id: item for item in images}
    selected = by_id.get(requested)
    if selected is not None:
        return selected

    requested_cf = requested.casefold()
    for item in images:
        if item.name.casefold() == requested_cf:
            return item

    stem_matches = [
        item
        for item in images
        if Path(item.name).stem.casefold() == requested_cf
    ]
    if len(stem_matches) == 1:
        return stem_matches[0]
    if len(stem_matches) > 1:
        names = ", ".join(repr(item.name) for item in stem_matches)
        raise EventImageError(
            f"Image selector {requested!r} matches multiple files in "
            f"{folder_name!r}: {names}"
        )

    available = ", ".join(
        f"{item.name} ({item.id})" for item in images
    ) or "(none)"
    raise EventImageError(
        f"Image selector {requested!r} is not in the {folder_name!r} Drive folder. "
        f"Use a Drive file id, filename (e.g. 1.jpg), or number (e.g. 1). "
        f"Available: {available}"
    )


def choose_facebook_image(
    drive_client: GoogleDriveClient,
    *,
    event_type: str,
    events_root: Path,
    image_id: str | None = None,
    folder_id: str = EVENT_IMAGES_DRIVE_FOLDER_ID,
    persist_rotation: bool = True,
) -> tuple[DriveFile, str]:
    """Pick a programme image by explicit Drive id/name or round-robin rotation.

    Default selection skips every image already used in the current cycle, including
    images previously chosen explicitly by the user. After all images have been used
    once in the cycle, selection starts over.

    Returns ``(drive_file, selection_mode)`` where selection_mode is
    ``\"explicit\"`` or ``\"rotation\"``.
    """
    normalized = normalize_event_type(event_type)
    images = list_programme_images(
        drive_client,
        event_type=normalized,
        folder_id=folder_id,
    )
    by_id = {item.id: item for item in images}
    image_ids = set(by_id)

    state = load_image_rotation_state(events_root)
    event_history = usage_history_from_events(load_events(events_root), event_type=normalized)
    history = merge_usage_history(state.get(normalized, []), event_history)
    used = used_ids_in_current_cycle(history, image_ids=image_ids)

    requested = (image_id or "").strip()
    if requested:
        selected = resolve_image_selector(
            images,
            selector=requested,
            folder_name=get_program(normalized).facebook_image_folder,
        )
        selection = "explicit"
    else:
        available = [item for item in images if item.id not in set(used)]
        if not available:
            # Cycle complete (or empty used after wrap); start over by name order.
            available = list(images)
        selected = available[0]
        selection = "rotation"

    history.append(selected.id)
    state[normalized] = history
    if persist_rotation:
        save_image_rotation_state(events_root, state)
    return selected, selection


def resolve_facebook_image_from_drive(
    *,
    project_root: Path,
    events_root: Path,
    event_type: str = EVENT_TYPE_SURYA_KRIYA,
    drive_client: GoogleDriveClient,
    folder_id: str = EVENT_IMAGES_DRIVE_FOLDER_ID,
    image_id: str | None = None,
    persist_rotation: bool = True,
) -> SelectedEventImage:
    """Select and download a Facebook event image from the programme Drive folder."""
    selected, selection = choose_facebook_image(
        drive_client,
        event_type=event_type,
        events_root=events_root,
        image_id=image_id,
        folder_id=folder_id,
        persist_rotation=persist_rotation,
    )
    cache_dir = event_image_cache_dir(project_root) / normalize_event_type(event_type)
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _SAFE_FILENAME_RE.sub("_", selected.name).strip("._") or "image.jpg"
    destination = cache_dir / f"{selected.id}_{safe_name}"
    try:
        local_path = drive_client.download_file(selected.id, destination)
    except GoogleDriveError as exc:
        raise EventImageError(
            f"Failed to download {selected.name!r} ({selected.id}) from Drive: {exc}"
        ) from exc
    return SelectedEventImage(
        drive_file=selected,
        local_path=local_path,
        selection=selection,
    )


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
