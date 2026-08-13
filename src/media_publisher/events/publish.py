from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from media_publisher.events.facebook_event import (
    EventImageError,
    publish_event_to_facebook,
    resolve_facebook_image_from_drive,
)
from media_publisher.events.drive_copy import EventTemplateError, load_program_from_drive
from media_publisher.events.format import parse_event_date, parse_event_time
from media_publisher.events.page import (
    StoredEvent,
    append_event,
    default_events_root,
    event_dedupe_key,
    find_duplicate,
    prune_past_events,
    rebuild_index,
    stored_event_from_dict,
)
from media_publisher.events.templates import (
    EVENT_TYPE_BHUTA_SHUDDHI,
    EVENT_TYPE_SURYA_KRIYA,
    EVENT_TYPE_YOGASANA,
    RenderedEvent,
    render_event,
    supported_event_types,
)
from media_publisher.publishers.meta import MetaAccessTokenInfo, MetaClient, MetaError, inspect_access_token
from media_publisher.sources.google_drive import GoogleDriveClient

REQUIRED_EVENT_META_SCOPES = (
    "pages_manage_posts",
)


class EventPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class EventPublishResult:
    rendered: RenderedEvent
    stored: StoredEvent | None
    created_on_page: bool
    dry_run: bool
    facebook_post_id: str | None = None
    facebook_permalink: str | None = None
    skipped_duplicate: bool = False
    pruned_count: int = 0
    facebook_image_id: str | None = None
    facebook_image_name: str | None = None
    facebook_image_selection: str | None = None


def prune_events_site(events_root: Path) -> tuple[list[dict], list[dict]]:
    return prune_past_events(events_root, write=True)


def check_event_meta_scopes(
    *,
    access_token: str,
    app_id: str,
    app_secret: str,
    api_version: str,
) -> tuple[MetaAccessTokenInfo, list[str]]:
    info = inspect_access_token(
        access_token,
        app_id=app_id,
        app_secret=app_secret,
        api_version=api_version,
    )
    granted = {scope.casefold() for scope in info.scopes}
    missing = [scope for scope in REQUIRED_EVENT_META_SCOPES if scope.casefold() not in granted]
    return info, missing


def publish_event(
    *,
    event_type: str,
    city: str,
    country: str,
    date_text: str,
    time_text: str,
    registration_link: str,
    project_root: Path,
    events_root: Path | None = None,
    dry_run: bool = False,
    skip_facebook: bool = False,
    meta_client: MetaClient | None = None,
    page_id: str | None = None,
    drive_client: GoogleDriveClient | None = None,
    image_id: str | None = None,
) -> EventPublishResult:
    try:
        event_date = parse_event_date(date_text)
        event_time = parse_event_time(time_text)
        program = None
        if drive_client is not None:
            program = load_program_from_drive(
                drive_client,
                event_type,
                project_root=project_root,
            )
        rendered = render_event(
            event_type=event_type,
            city=city,
            country=country,
            event_date=event_date,
            event_time=event_time,
            registration_link=registration_link,
            program=program,
        )
    except (ValueError, EventTemplateError) as exc:
        raise EventPublishError(str(exc)) from exc

    root = events_root or default_events_root(project_root)

    if dry_run:
        return EventPublishResult(
            rendered=rendered,
            stored=None,
            created_on_page=False,
            dry_run=True,
        )

    existing: list = []
    pruned_count = 0
    if root.is_dir():
        existing, removed = prune_past_events(root, write=True)
        pruned_count = len(removed)
    duplicate = find_duplicate(existing, event_dedupe_key(rendered))
    if duplicate is not None:
        stored = stored_event_from_dict(duplicate)
        rebuild_index(root, existing)
        return EventPublishResult(
            rendered=rendered,
            stored=stored,
            created_on_page=False,
            dry_run=False,
            facebook_post_id=stored.facebook_post_id,
            facebook_permalink=stored.facebook_permalink,
            skipped_duplicate=True,
            pruned_count=pruned_count,
        )

    facebook_post_id: str | None = None
    facebook_permalink: str | None = None
    facebook_image_id: str | None = None
    facebook_image_name: str | None = None
    facebook_image_selection: str | None = None

    if not skip_facebook:
        if meta_client is None or not page_id:
            raise EventPublishError(
                "Meta client and page_id are required unless dry_run or skip_facebook is set"
            )
        if drive_client is None:
            raise EventPublishError(
                "Google Drive client is required to download the Facebook event image"
            )
        try:
            selected = resolve_facebook_image_from_drive(
                project_root=project_root,
                events_root=root,
                event_type=rendered.event_type,
                drive_client=drive_client,
                image_id=image_id,
            )
            facebook_image_id = selected.drive_file.id
            facebook_image_name = selected.drive_file.name
            facebook_image_selection = selected.selection
            facebook_post_id, facebook_permalink = publish_event_to_facebook(
                meta_client,
                page_id=page_id,
                rendered=rendered,
                image_path=selected.local_path,
            )
        except (MetaError, EventImageError) as exc:
            raise EventPublishError(str(exc)) from exc

    stored, created = append_event(
        root,
        rendered,
        facebook_post_id=facebook_post_id,
        facebook_permalink=facebook_permalink,
        facebook_image_id=facebook_image_id,
        facebook_image_name=facebook_image_name,
    )
    return EventPublishResult(
        rendered=rendered,
        stored=stored,
        created_on_page=created,
        dry_run=False,
        facebook_post_id=facebook_post_id,
        facebook_permalink=facebook_permalink,
        skipped_duplicate=not created,
        pruned_count=pruned_count,
        facebook_image_id=facebook_image_id,
        facebook_image_name=facebook_image_name,
        facebook_image_selection=facebook_image_selection,
    )


__all__ = [
    "EVENT_TYPE_BHUTA_SHUDDHI",
    "EVENT_TYPE_SURYA_KRIYA",
    "EVENT_TYPE_YOGASANA",
    "EventPublishError",
    "EventPublishResult",
    "REQUIRED_EVENT_META_SCOPES",
    "check_event_meta_scopes",
    "prune_events_site",
    "publish_event",
    "supported_event_types",
]
