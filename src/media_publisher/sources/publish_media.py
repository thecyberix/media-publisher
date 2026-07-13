"""Resolve publish-time Bulgarian thumbnails and optional Drive video overrides.

Original platform thumbnails belong in Airtable (uploaded on catalog ingest).
Publishing uses a translated thumbnail only when Original Video Thumbnail is set:

1. Drive override folder (Thumbnails subfolder), by Title
2. Canva catalog folder (short vs long), by Title — move to Published on success
3. TN render generated on the fly

Video source for publishing (independent of thumbnail logic):

1. Drive override folder (Videos subfolder), by Title
2. HappyScribe download (handled by the publish pipeline)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from media_publisher.models import PublishJob
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    has_original_video_thumbnail,
)
from media_publisher.sources.canva import (
    CanvaClient,
    CanvaError,
    CanvaThumbnailTarget,
    catalog_video_name_from_job,
    find_cached_thumbnail_path,
    parse_canva_resource,
    thumbnail_catalog_url_for_format,
    thumbnail_destination_path,
)
from media_publisher.sources.google_drive import (
    GoogleDriveClient,
    GoogleDriveError,
    IMAGE_MIME_PREFIX,
    VIDEO_MIME_PREFIX,
)
from media_publisher.sources.tn_publish import (
    TnPublishError,
    TnPublishSettings,
    generate_catalog_tn_thumbnail,
)

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


@dataclass
class PublishMediaCleanup:
    drive_file_ids_to_delete: list[str] = field(default_factory=list)
    canva_design_id: str | None = None
    canva_published_folder_id: str | None = None


@dataclass(frozen=True)
class PublishThumbnailResult:
    path: Path | None
    source: str | None = None
    cleanup: PublishMediaCleanup | None = None


@dataclass(frozen=True)
class PublishVideoResult:
    path: Path | None
    source: str | None = None
    cleanup: PublishMediaCleanup | None = None


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "file"


def merge_publish_media_cleanup(
    left: PublishMediaCleanup | None,
    right: PublishMediaCleanup | None,
) -> PublishMediaCleanup | None:
    if left is None and right is None:
        return None
    merged = PublishMediaCleanup()
    if left is not None:
        merged.drive_file_ids_to_delete.extend(left.drive_file_ids_to_delete)
        merged.canva_design_id = left.canva_design_id or merged.canva_design_id
        merged.canva_published_folder_id = (
            left.canva_published_folder_id or merged.canva_published_folder_id
        )
    if right is not None:
        merged.drive_file_ids_to_delete.extend(right.drive_file_ids_to_delete)
        merged.canva_design_id = right.canva_design_id or merged.canva_design_id
        merged.canva_published_folder_id = (
            right.canva_published_folder_id or merged.canva_published_folder_id
        )
    if not merged.drive_file_ids_to_delete and not merged.canva_design_id:
        return None
    return merged


def resolve_drive_override_subfolder(
    drive: GoogleDriveClient,
    *,
    root_folder_id: str,
    subfolder_name: str,
) -> str | None:
    folder = drive.find_child_folder(root_folder_id, subfolder_name)
    return folder.id if folder else None


def _override_match_stem(name: str) -> str:
    stem = Path(name).stem
    if stem.casefold().endswith(".tn-render"):
        stem = stem[: -len(".tn-render")]
    return _sanitize_filename(stem).casefold().strip()


def resolve_drive_override_thumbnail(
    drive: GoogleDriveClient,
    *,
    root_folder_id: str,
    thumbnails_subfolder: str,
    title: str,
    download_dir: Path,
) -> PublishThumbnailResult | None:
    folder_id = resolve_drive_override_subfolder(
        drive,
        root_folder_id=root_folder_id,
        subfolder_name=thumbnails_subfolder,
    )
    if folder_id is None:
        return None

    target = _override_match_stem(title)
    if not target:
        return None

    match = None
    for item in drive.list_children(folder_id):
        if item.mime_type == "application/vnd.google-apps.folder":
            continue
        if _override_match_stem(item.name) != target:
            continue
        if not item.mime_type.startswith(IMAGE_MIME_PREFIX):
            if Path(item.name).suffix.casefold() not in IMAGE_EXTENSIONS:
                continue
        match = item
        break
    if match is None:
        return None

    destination = download_dir / "overrides" / "thumbnails" / _sanitize_filename(match.name)
    drive.download_file(match.id, destination)
    cleanup = PublishMediaCleanup(drive_file_ids_to_delete=[match.id])
    return PublishThumbnailResult(
        path=destination,
        source="drive-override-thumbnail",
        cleanup=cleanup,
    )


def resolve_canva_catalog_thumbnail(
    job: PublishJob,
    *,
    client: CanvaClient,
    title: str,
    download_dir: Path,
    long_catalog_url: str,
    short_catalog_url: str,
    published_subfolder_name: str,
    move_to_published: bool = True,
) -> PublishThumbnailResult | None:
    cached = find_cached_thumbnail_path(download_dir, title)
    if cached is not None:
        return PublishThumbnailResult(path=cached, source="cached-canva")

    catalog_ref = thumbnail_catalog_url_for_format(
        job.video_format,
        long_url=long_catalog_url,
        short_url=short_catalog_url,
    )
    resource_type, resource_id = parse_canva_resource(catalog_ref)
    if resource_type != "folder":
        return None

    try:
        design = client.find_design_in_folder(resource_id, title)
    except CanvaError:
        return None

    destination = thumbnail_destination_path(download_dir, title)
    target = CanvaThumbnailTarget(design_id=design.id)
    client.download_thumbnail_target(target, destination)

    cleanup: PublishMediaCleanup | None = None
    if move_to_published:
        published_folder = client.find_subfolder(resource_id, published_subfolder_name)
        if published_folder is not None:
            cleanup = PublishMediaCleanup(
                canva_design_id=design.id,
                canva_published_folder_id=published_folder.id,
            )

    return PublishThumbnailResult(
        path=destination,
        source="canva-catalog",
        cleanup=cleanup,
    )


def resolve_generated_tn_thumbnail(
    *,
    title: str,
    record_fields: dict[str, Any],
    drive: GoogleDriveClient,
    tn_settings: TnPublishSettings,
) -> PublishThumbnailResult:
    path = generate_catalog_tn_thumbnail(
        title=title,
        record_fields=record_fields,
        drive=drive,
        settings=tn_settings,
    )
    return PublishThumbnailResult(path=path, source="tn-generated")


def resolve_publish_thumbnail(
    job: PublishJob,
    record_fields: dict[str, Any],
    *,
    title: str,
    canva_client: CanvaClient | None,
    drive: GoogleDriveClient | None,
    canva_download_dir: Path,
    long_catalog_url: str,
    short_catalog_url: str,
    override_root_folder_id: str,
    thumbnails_subfolder: str,
    published_subfolder_name: str,
    tn_settings: TnPublishSettings,
) -> PublishThumbnailResult:
    """Resolve a Bulgarian publish thumbnail when Original Video Thumbnail is set.

    The Airtable attachment itself is not used at publish time; it only gates
    whether a translated thumbnail is required.
    """
    if not has_original_video_thumbnail(record_fields):
        return PublishThumbnailResult(path=None)

    lookup_title = title.strip() or catalog_video_name_from_job(job)

    if drive is not None and override_root_folder_id:
        drive_result = resolve_drive_override_thumbnail(
            drive,
            root_folder_id=override_root_folder_id,
            thumbnails_subfolder=thumbnails_subfolder,
            title=lookup_title,
            download_dir=canva_download_dir,
        )
        if drive_result is not None and drive_result.path is not None:
            return drive_result

    if canva_client is not None:
        canva_result = resolve_canva_catalog_thumbnail(
            job,
            client=canva_client,
            title=lookup_title,
            download_dir=canva_download_dir,
            long_catalog_url=long_catalog_url,
            short_catalog_url=short_catalog_url,
            published_subfolder_name=published_subfolder_name,
        )
        if canva_result is not None and canva_result.path is not None:
            return canva_result

    if drive is not None:
        generated = resolve_generated_tn_thumbnail(
            title=lookup_title,
            record_fields=record_fields,
            drive=drive,
            tn_settings=tn_settings,
        )
        if generated.path is not None:
            return generated

    raise TnPublishError(
        f"Could not resolve translated thumbnail for {lookup_title!r}"
    )


def resolve_drive_override_video(
    drive: GoogleDriveClient,
    *,
    root_folder_id: str,
    videos_subfolder: str,
    title: str,
    download_dir: Path,
) -> PublishVideoResult | None:
    folder_id = resolve_drive_override_subfolder(
        drive,
        root_folder_id=root_folder_id,
        subfolder_name=videos_subfolder,
    )
    if folder_id is None:
        return None

    target = _override_match_stem(title)
    if not target:
        return None

    match = None
    for item in drive.list_children(folder_id):
        if item.mime_type == "application/vnd.google-apps.folder":
            continue
        if _override_match_stem(item.name) != target:
            continue
        if not item.mime_type.startswith(VIDEO_MIME_PREFIX):
            if Path(item.name).suffix.casefold() not in VIDEO_EXTENSIONS:
                continue
        match = item
        break
    if match is None:
        return None

    destination = download_dir / "overrides" / "videos" / _sanitize_filename(match.name)
    drive.download_file(match.id, destination)
    cleanup = PublishMediaCleanup(drive_file_ids_to_delete=[match.id])
    return PublishVideoResult(
        path=destination,
        source="drive-override-video",
        cleanup=cleanup,
    )


def resolve_publish_video(
    *,
    title: str,
    drive: GoogleDriveClient | None,
    override_root_folder_id: str,
    videos_subfolder: str,
    download_dir: Path,
) -> PublishVideoResult:
    """Return a Drive override video when present; otherwise no local path."""
    lookup_title = title.strip()
    if not lookup_title:
        return PublishVideoResult(path=None)

    if drive is None or not override_root_folder_id:
        return PublishVideoResult(path=None)

    override = resolve_drive_override_video(
        drive,
        root_folder_id=override_root_folder_id,
        videos_subfolder=videos_subfolder,
        title=lookup_title,
        download_dir=download_dir,
    )
    if override is not None:
        return override
    return PublishVideoResult(path=None)


def apply_publish_media_cleanup(
    cleanup: PublishMediaCleanup | None,
    *,
    drive: GoogleDriveClient | None,
    canva_client: CanvaClient | None,
    log: Callable[[str], None] | None = None,
) -> None:
    if cleanup is None:
        return
    emit = log or (lambda message: None)

    if drive is not None:
        for file_id in cleanup.drive_file_ids_to_delete:
            try:
                drive.delete_file(file_id)
                emit(f"  cleanup: deleted Drive override file {file_id}")
            except GoogleDriveError as exc:
                emit(f"  cleanup: failed to delete Drive file {file_id}: {exc}")

    if (
        canva_client is not None
        and cleanup.canva_design_id
        and cleanup.canva_published_folder_id
    ):
        try:
            canva_client.move_folder_item(
                item_id=cleanup.canva_design_id,
                to_folder_id=cleanup.canva_published_folder_id,
            )
            emit(
                "  cleanup: moved Canva design "
                f"{cleanup.canva_design_id} to Published folder"
            )
        except CanvaError as exc:
            emit(f"  cleanup: failed to move Canva design: {exc}")
