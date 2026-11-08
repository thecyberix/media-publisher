"""Schedule prepared Drive quote JPEGs as YouTube community posts.

Discovery matches the Facebook prepared-quote scheduler: Drive
``Quotes/{MM Mon YYYY}/`` images paired with Ready sheet captions. Publishing
uses the youtube.com composer, because the Data API cannot create community
posts. History lives in ``downloads/quotes/youtube-community-schedule-history.json``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from media_publisher.publishers.youtube_web import (
    CommunityImagePost,
    schedule_community_image_posts,
)
from media_publisher.quotes_facebook_browser_schedule import (
    PreparedQuoteScheduleItem,
    mark_scheduled_in_history,
    save_schedule_history,
)

HISTORY_RELATIVE_PATH = Path("downloads/quotes/youtube-community-schedule-history.json")


def default_history_path(project_root: Path) -> Path:
    return project_root / HISTORY_RELATIVE_PATH


def schedule_prepared_quotes_via_browser(
    items: list[PreparedQuoteScheduleItem],
    *,
    channel_handle: str,
    project_root: Path,
    history: dict[str, dict[str, Any]],
    history_path: Path,
    display_timezone: str,
    dry_run: bool = False,
    print_line: Callable[[str], None] | None = None,
    publisher: Callable[..., tuple[int, int, list[str]]] | None = None,
) -> tuple[int, int, list[str]]:
    """Schedule items and record each success in ``history``. Returns (ok, failed, errors)."""
    log = print_line or print
    if dry_run:
        for item in items:
            log(
                f"{item.stem}: schedule {item.publish_at.isoformat()} "
                f"({item.image_path.name})"
            )
            preview = item.caption if len(item.caption) <= 80 else item.caption[:77] + "..."
            log(f"  dry-run caption: {preview}")
        return len(items), 0, []

    by_stem = {item.stem: item for item in items}
    posts = [
        CommunityImagePost(
            stem=item.stem,
            image_path=item.image_path,
            caption=item.caption,
            publish_at=item.publish_at,
            failure_screenshot=(
                project_root
                / "downloads"
                / f"youtube-community-schedule-failure-{item.stem}.png"
            ),
        )
        for item in items
    ]

    def on_success(post: CommunityImagePost, permalink: str) -> None:
        item = by_stem[post.stem]
        mark_scheduled_in_history(
            history,
            stem=item.stem,
            permalink=permalink,
            publish_at=item.publish_at,
            content_fingerprint=item.content_fingerprint,
            drive_file_id=item.drive_file_id,
        )
        save_schedule_history(history_path, history)
        log(f"  scheduled ({permalink})")

    publish = publisher or schedule_community_image_posts
    return publish(
        posts,
        channel_handle=channel_handle,
        display_timezone=display_timezone,
        storage_state_path=_state_path(project_root),
        browser_profile_dir=_profile_dir(project_root),
        browser_channel=_browser_channel(),
        headless=None,
        on_success=on_success,
        print_line=log,
    )


def _state_path(project_root: Path) -> Path:
    from media_publisher.publishers.youtube_web import resolve_youtube_browser_state_path

    return resolve_youtube_browser_state_path(project_root=project_root)


def _profile_dir(project_root: Path) -> Path:
    from media_publisher.publishers.youtube_web import resolve_youtube_browser_profile_dir

    return resolve_youtube_browser_profile_dir(project_root=project_root)


def _browser_channel() -> str | None:
    from media_publisher.publishers.youtube_web import resolve_youtube_browser_channel

    return resolve_youtube_browser_channel()
