"""Publish the Ekadashi photo that is due today.

The post goes live the calendar day before the observance, the same moment the
12:30 Europe/Sofia job runs. Facebook and Instagram get the still. YouTube gets
a 10-second video made from that still and is added to the Ekadashi playlist.
A committed state file keeps a second run from posting again.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from catalog_parser.playlists import load_playlist_config
from media_publisher.ekadashi import (
    EkadashiCalendarError,
    EkadashiEntry,
    ekadashi_entries_due_on,
    ekadashi_entries_for_year,
)
from media_publisher.ekadashi_facebook_browser_schedule import (
    EkadashiFacebookScheduleError,
    export_ekadashi_page,
    find_ekadashi_design,
)
from media_publisher.models import PublishJob
from media_publisher.post_templates import build_ekadashi_youtube_title
from media_publisher.publishers.facebook import FacebookPublishError
from media_publisher.publishers.instagram import InstagramPublishError
from media_publisher.publishers.meta import MetaError
from media_publisher.publishers.quotes import (
    publish_local_quote_to_facebook,
    publish_local_quote_to_instagram,
)
from media_publisher.publishers.youtube import YouTubePublishError, publish_to_youtube, youtube_video_url
from media_publisher.sources.canva import CanvaClient, CanvaError
from media_publisher.sources.image_video import ImageVideoError, ensure_quote_video
from media_publisher.timezones import get_timezone

PLATFORMS = ("facebook", "instagram", "youtube")
STATE_RELATIVE_PATH = Path("data/ekadashi_publish_state.json")
CACHE_RELATIVE_DIR = Path("downloads/ekadashi/publish-cache")


def default_state_path(project_root: Path) -> Path:
    return project_root / STATE_RELATIVE_PATH


def load_publish_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise EkadashiFacebookScheduleError(f"Invalid Ekadashi publish state: {path}")
    return payload


def save_publish_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def pending_platforms(
    state: dict[str, dict[str, Any]],
    stem: str,
    platforms: tuple[str, ...],
) -> list[str]:
    entry = state.get(stem)
    recorded = entry.get("platforms") if isinstance(entry, dict) else None
    if not isinstance(recorded, dict):
        recorded = {}
    pending: list[str] = []
    for platform in platforms:
        record = recorded.get(platform)
        permalink = record.get("permalink") if isinstance(record, dict) else None
        if not isinstance(permalink, str) or not permalink.strip():
            pending.append(platform)
    return pending


def mark_platform_published(
    state: dict[str, dict[str, Any]],
    *,
    stem: str,
    platform: str,
    permalink: str,
) -> None:
    entry = dict(state.get(stem) or {})
    recorded = entry.get("platforms")
    platforms = dict(recorded) if isinstance(recorded, dict) else {}
    platforms[platform] = {"permalink": permalink}
    entry["platforms"] = platforms
    state[stem] = entry


def run_ekadashi_publish(
    *,
    project_root: Path,
    publish_timezone: str,
    canva_client: CanvaClient,
    page_id: str,
    instagram_account_id: str,
    access_token: str,
    meta_app_id: str | None = None,
    youtube_client_secrets: Path | None = None,
    youtube_token: Path | None = None,
    youtube_channel_handle: str | None = None,
    ffmpeg_path: str | None = None,
    template_urls: dict[str, str] | None = None,
    today: date | None = None,
    days_before: int = 1,
    platforms: tuple[str, ...] = PLATFORMS,
    dry_run: bool = False,
    state_path: Path | None = None,
    cache_dir: Path | None = None,
    ics_text: str | None = None,
    ics_path: Path | None = None,
    entries: tuple[EkadashiEntry, ...] | None = None,
    print_line: Callable[[str], None] = print,
) -> int:
    """Publish Facebook and Instagram photos due on ``today``. Returns an exit code."""
    if today is None:
        today = datetime.now(get_timezone(publish_timezone)).date()
    selected = platforms or PLATFORMS
    unknown = [platform for platform in selected if platform not in PLATFORMS]
    if unknown:
        print_line(
            "Ekadashi posts publish to Facebook and Instagram. "
            f"Unsupported: {', '.join(unknown)}"
        )
        return 1

    try:
        year_entries = (
            entries
            if entries is not None
            else ekadashi_entries_for_year(
                (today + timedelta(days=days_before)).year,
                ics_text=ics_text,
                ics_path=ics_path,
            )
        )
        due = ekadashi_entries_due_on(today, year_entries, days_before=days_before)
    except (EkadashiCalendarError, ValueError) as exc:
        print_line(str(exc))
        return 1

    if not due:
        print_line(f"No Ekadashi post is due on {today.isoformat()}.")
        return 0

    history_path = state_path or default_state_path(project_root)
    state = load_publish_state(history_path)
    image_dir = cache_dir or (project_root / CACHE_RELATIVE_DIR)
    failed = False

    for entry in due:
        waiting = pending_platforms(state, entry.stem, selected)
        if not waiting:
            print_line(f"{entry.stem}: already published")
            continue
        print_line(
            f"{entry.stem}: {entry.name} page {entry.page_number} -> "
            + ", ".join(waiting)
        )
        if dry_run:
            continue

        try:
            design = find_ekadashi_design(canva_client, year=entry.year)
            image_path = export_ekadashi_page(
                canva_client,
                design_id=design.design_id,
                page_number=entry.page_number,
                destination=image_dir / f"{entry.stem}.jpg",
            )
        except (EkadashiFacebookScheduleError, CanvaError) as exc:
            print_line(f"{entry.stem}: image export failed — {exc}")
            failed = True
            continue

        for platform in waiting:
            try:
                permalink = _publish_platform(
                    platform,
                    image_path=image_path,
                    caption=entry.caption,
                    page_id=page_id,
                    instagram_account_id=instagram_account_id,
                    access_token=access_token,
                    meta_app_id=meta_app_id,
                    project_root=project_root,
                    youtube_client_secrets=youtube_client_secrets,
                    youtube_token=youtube_token,
                    youtube_channel_handle=youtube_channel_handle,
                    ffmpeg_path=ffmpeg_path,
                    template_urls=template_urls,
                )
            except (
                FacebookPublishError,
                InstagramPublishError,
                MetaError,
                YouTubePublishError,
                ImageVideoError,
            ) as exc:
                print_line(f"  {platform}: failed — {exc}")
                failed = True
                continue
            mark_platform_published(
                state,
                stem=entry.stem,
                platform=platform,
                permalink=permalink,
            )
            save_publish_state(history_path, state)
            print_line(f"  {platform}: published ({permalink})")

    return 1 if failed else 0


def publish_ekadashi_to_youtube(
    *,
    image_path: Path,
    caption: str,
    client_secrets_path: Path,
    token_path: Path,
    expected_channel_handle: str | None,
    work_dir: Path,
    ffmpeg_path: str | None = None,
    template_urls: dict[str, str] | None = None,
    playlist_title: str | None = None,
) -> str:
    """Upload a 10-second video of the still and add it to the Ekadashi playlist."""
    video_path = ensure_quote_video(image_path, work_dir, ffmpeg_path=ffmpeg_path)
    title = build_ekadashi_youtube_title(caption)
    description = caption.strip()
    job = PublishJob(
        title=title,
        description=description,
        video_path=str(video_path),
        thumbnail_path=str(image_path),
        video_format="short_form",
        content_kind="image",
    )
    from media_publisher.scheduling import prepare_job_for_immediate_publish

    prepare_job_for_immediate_publish(job, private=False)
    resolved_title = (playlist_title or "").strip() or (
        load_playlist_config().ekadashi_playlist or ""
    )
    if not resolved_title:
        raise YouTubePublishError("ekadashi_playlist is not configured")
    video_id = publish_to_youtube(
        job,
        client_secrets_path=client_secrets_path,
        token_path=token_path,
        expected_channel_handle=expected_channel_handle,
        ffmpeg_path=ffmpeg_path,
        quote_playlist_title=resolved_title,
        **(template_urls or {}),
    )
    return youtube_video_url(video_id)


def _publish_platform(
    platform: str,
    *,
    image_path: Path,
    caption: str,
    page_id: str,
    instagram_account_id: str,
    access_token: str,
    meta_app_id: str | None,
    project_root: Path,
    youtube_client_secrets: Path | None,
    youtube_token: Path | None,
    youtube_channel_handle: str | None,
    ffmpeg_path: str | None,
    template_urls: dict[str, str] | None,
) -> str:
    if platform == "facebook":
        return publish_local_quote_to_facebook(
            image_path=image_path,
            caption=caption,
            publish_at=None,
            page_id=page_id,
            access_token=access_token,
        )
    if platform == "instagram":
        return publish_local_quote_to_instagram(
            image_path=image_path,
            caption=caption,
            publish_at=None,
            page_id=page_id,
            instagram_account_id=instagram_account_id,
            access_token=access_token,
            app_id=meta_app_id,
        )
    if platform == "youtube":
        if youtube_client_secrets is None or youtube_token is None:
            raise YouTubePublishError("YouTube Ekadashi publishing is not configured")
        return publish_ekadashi_to_youtube(
            image_path=image_path,
            caption=caption,
            client_secrets_path=youtube_client_secrets,
            token_path=youtube_token,
            expected_channel_handle=youtube_channel_handle,
            work_dir=project_root / CACHE_RELATIVE_DIR,
            ffmpeg_path=ffmpeg_path,
            template_urls=template_urls,
        )
    raise ValueError(f"Unsupported platform {platform!r}")
