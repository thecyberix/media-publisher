"""Resolve publish-time Bulgarian thumbnails and optional Drive video overrides.

Original platform thumbnails belong in Airtable (uploaded on catalog ingest).
Publishing uses a translated thumbnail only when Original Video Thumbnail is set:

1. Drive override folder (Thumbnails and Thumbnails/Published), by Title — move to Published on success
2. Canva catalog folder (short vs long, including Published), by Title — move to Published on success
3. TN render generated on the fly

Video source for publishing (independent of thumbnail logic):

1. Drive override folder (Videos subfolder), by Title — delete on success
2. When Translation resources are empty: Combined Media File (must already exist)
3. Otherwise: HappyScribe download with burned subtitles (handled by the publish pipeline)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from media_publisher.models import PublishJob
from media_publisher.sources.airtable import (
    FIELD_COMBINED_MEDIA_FILE,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_TITLE,
    has_original_video_thumbnail,
)
from media_publisher.sources.canva import (
    CanvaClient,
    CanvaError,
    CanvaThumbnailTarget,
    catalog_video_name_from_job,
    find_cached_thumbnail_path,
    is_canva_auth_error,
    parse_canva_resource,
    thumbnail_catalog_url_for_format,
    thumbnail_destination_path,
)
from media_publisher.sources.google_drive import (
    DriveFile,
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


@dataclass(frozen=True)
class DriveFileMove:
    file_id: str
    destination_folder_id: str


@dataclass
class PublishMediaCleanup:
    drive_file_ids_to_delete: list[str] = field(default_factory=list)
    drive_file_moves: list[DriveFileMove] = field(default_factory=list)
    canva_design_id: str | None = None
    canva_published_folder_id: str | None = None
    combined_media_file_id: str | None = None


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


def extract_drive_file_id(value: str) -> str | None:
    if not value:
        return None
    text = value.strip()
    match = re.search(r"/file/d/([^/]+)", text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


class CombinedMediaError(Exception):
    """Raised when Combined Media File cannot be resolved for publish."""


def combined_media_file_id_from_fields(record_fields: dict[str, Any]) -> str | None:
    combined = record_fields.get(FIELD_COMBINED_MEDIA_FILE)
    if combined is None:
        return None
    if not isinstance(combined, str):
        combined = str(combined)
    if not combined.strip():
        return None
    return extract_drive_file_id(combined)


def combined_media_cleanup_from_fields(
    record_fields: dict[str, Any],
) -> PublishMediaCleanup | None:
    file_id = combined_media_file_id_from_fields(record_fields)
    if not file_id:
        return None
    return PublishMediaCleanup(combined_media_file_id=file_id)


def resolve_combined_media_for_publish(
    *,
    record_fields: dict[str, Any],
    drive: GoogleDriveClient,
    download_dir: Path,
) -> PublishVideoResult:
    """Download an existing Combined Media File for publish (no generation)."""
    title = record_fields.get(FIELD_TITLE)
    if not isinstance(title, str) or not title.strip():
        raise CombinedMediaError(f"Missing {FIELD_TITLE!r} for Combined Media File")

    existing_id = combined_media_file_id_from_fields(record_fields)
    if not existing_id:
        raise CombinedMediaError(f"Missing {FIELD_COMBINED_MEDIA_FILE!r}")

    output_name = title.strip()
    if not output_name.casefold().endswith(".mp4"):
        output_name = f"{output_name}.mp4"
    destination = download_dir / "combined" / _sanitize_filename(output_name)
    drive.download_file(existing_id, destination)
    return PublishVideoResult(
        path=destination,
        source="combined-media",
        cleanup=PublishMediaCleanup(combined_media_file_id=existing_id),
    )


def merge_publish_media_cleanup(
    left: PublishMediaCleanup | None,
    right: PublishMediaCleanup | None,
) -> PublishMediaCleanup | None:
    if left is None and right is None:
        return None
    merged = PublishMediaCleanup()
    if left is not None:
        merged.drive_file_ids_to_delete.extend(left.drive_file_ids_to_delete)
        merged.drive_file_moves.extend(left.drive_file_moves)
        merged.canva_design_id = left.canva_design_id or merged.canva_design_id
        merged.canva_published_folder_id = (
            left.canva_published_folder_id or merged.canva_published_folder_id
        )
        merged.combined_media_file_id = (
            left.combined_media_file_id or merged.combined_media_file_id
        )
    if right is not None:
        merged.drive_file_ids_to_delete.extend(right.drive_file_ids_to_delete)
        merged.drive_file_moves.extend(right.drive_file_moves)
        merged.canva_design_id = right.canva_design_id or merged.canva_design_id
        merged.canva_published_folder_id = (
            right.canva_published_folder_id or merged.canva_published_folder_id
        )
        merged.combined_media_file_id = (
            right.combined_media_file_id or merged.combined_media_file_id
        )
    if (
        not merged.drive_file_ids_to_delete
        and not merged.drive_file_moves
        and not merged.canva_design_id
        and not merged.combined_media_file_id
    ):
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


def _find_drive_override_image(
    drive: GoogleDriveClient,
    folder_ids: list[str],
    target: str,
) -> tuple[DriveFile, str] | None:
    for folder_id in folder_ids:
        for item in drive.list_children(folder_id):
            if item.mime_type == "application/vnd.google-apps.folder":
                continue
            if _override_match_stem(item.name) != target:
                continue
            if not item.mime_type.startswith(IMAGE_MIME_PREFIX):
                if Path(item.name).suffix.casefold() not in IMAGE_EXTENSIONS:
                    continue
            return item, folder_id
    return None


def drive_override_thumbnail_exists(
    drive: GoogleDriveClient,
    *,
    root_folder_id: str,
    thumbnails_subfolder: str,
    published_subfolder_name: str,
    title: str,
) -> bool:
    """True when a matching image exists in Thumbnails or Thumbnails/Published."""
    folder_id = resolve_drive_override_subfolder(
        drive,
        root_folder_id=root_folder_id,
        subfolder_name=thumbnails_subfolder,
    )
    if folder_id is None:
        return False

    target = _override_match_stem(title)
    if not target:
        return False

    search_folder_ids = [folder_id]
    published_folder = drive.find_child_folder(folder_id, published_subfolder_name)
    if published_folder is not None:
        search_folder_ids.append(published_folder.id)

    return _find_drive_override_image(drive, search_folder_ids, target) is not None


def canva_catalog_thumbnail_exists(
    *,
    client: CanvaClient,
    title: str,
    video_format: str,
    long_catalog_url: str,
    short_catalog_url: str,
    published_subfolder_name: str,
) -> bool:
    """True when a Canva design matching title exists in the catalog or Published folder."""
    catalog_ref = thumbnail_catalog_url_for_format(
        video_format,
        long_url=long_catalog_url,
        short_url=short_catalog_url,
    )
    resource_type, resource_id = parse_canva_resource(catalog_ref)
    if resource_type != "folder":
        return False

    published_folder = client.find_subfolder(resource_id, published_subfolder_name)
    try:
        client.find_design_in_folder(resource_id, title)
        return True
    except CanvaError as exc:
        if is_canva_auth_error(exc):
            raise
        if published_folder is None:
            return False
        try:
            client.find_design_in_folder(published_folder.id, title)
            return True
        except CanvaError as nested:
            if is_canva_auth_error(nested):
                raise
            return False


def has_prepared_publish_thumbnail(
    *,
    title: str,
    video_format: str,
    drive: GoogleDriveClient | None,
    canva_client: CanvaClient | None,
    override_root_folder_id: str,
    thumbnails_subfolder: str,
    published_subfolder_name: str,
    long_catalog_url: str,
    short_catalog_url: str,
) -> bool:
    """True when Drive override or Canva catalog already has a prepared thumbnail."""
    if drive is not None and override_root_folder_id:
        if drive_override_thumbnail_exists(
            drive,
            root_folder_id=override_root_folder_id,
            thumbnails_subfolder=thumbnails_subfolder,
            published_subfolder_name=published_subfolder_name,
            title=title,
        ):
            return True

    if canva_client is not None:
        if canva_catalog_thumbnail_exists(
            client=canva_client,
            title=title,
            video_format=video_format,
            long_catalog_url=long_catalog_url,
            short_catalog_url=short_catalog_url,
            published_subfolder_name=published_subfolder_name,
        ):
            return True

    return False


def resolve_drive_override_thumbnail(
    drive: GoogleDriveClient,
    *,
    root_folder_id: str,
    thumbnails_subfolder: str,
    published_subfolder_name: str,
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

    search_folder_ids = [folder_id]
    published_folder = drive.find_child_folder(folder_id, published_subfolder_name)
    if published_folder is not None:
        search_folder_ids.append(published_folder.id)

    found = _find_drive_override_image(drive, search_folder_ids, target)
    if found is None:
        return None
    match, parent_folder_id = found

    destination = download_dir / "overrides" / "thumbnails" / _sanitize_filename(match.name)
    drive.download_file(match.id, destination)
    cleanup: PublishMediaCleanup | None = None
    if published_folder is not None and parent_folder_id == folder_id:
        cleanup = PublishMediaCleanup(
            drive_file_moves=[
                DriveFileMove(
                    file_id=match.id,
                    destination_folder_id=published_folder.id,
                )
            ]
        )
    source = (
        "drive-override-thumbnail-published"
        if published_folder is not None and parent_folder_id == published_folder.id
        else "drive-override-thumbnail"
    )
    return PublishThumbnailResult(
        path=destination,
        source=source,
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

    published_folder = client.find_subfolder(resource_id, published_subfolder_name)
    already_in_published = False
    try:
        design = client.find_design_in_folder(resource_id, title)
    except CanvaError as exc:
        if is_canva_auth_error(exc):
            raise
        if published_folder is None:
            raise
        design = client.find_design_in_folder(published_folder.id, title)
        already_in_published = True

    destination = thumbnail_destination_path(download_dir, title)
    target = CanvaThumbnailTarget(design_id=design.id)
    client.download_thumbnail_target(target, destination)

    cleanup: PublishMediaCleanup | None = None
    if move_to_published and not already_in_published and published_folder is not None:
        cleanup = PublishMediaCleanup(
            canva_design_id=design.id,
            canva_published_folder_id=published_folder.id,
        )

    return PublishThumbnailResult(
        path=destination,
        source=(
            "canva-catalog-published"
            if already_in_published
            else "canva-catalog"
        ),
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
    attempts: list[str] = []

    if drive is None or not override_root_folder_id:
        attempts.append("drive override: Google Drive client unavailable")
    else:
        drive_result = resolve_drive_override_thumbnail(
            drive,
            root_folder_id=override_root_folder_id,
            thumbnails_subfolder=thumbnails_subfolder,
            published_subfolder_name=published_subfolder_name,
            title=lookup_title,
            download_dir=canva_download_dir,
        )
        if drive_result is not None and drive_result.path is not None:
            return drive_result
        attempts.append("drive override: no matching file in Thumbnails or Published folder")

    if canva_client is None:
        attempts.append("canva catalog: Canva client unavailable")
    else:
        try:
            canva_result = resolve_canva_catalog_thumbnail(
                job,
                client=canva_client,
                title=lookup_title,
                download_dir=canva_download_dir,
                long_catalog_url=long_catalog_url,
                short_catalog_url=short_catalog_url,
                published_subfolder_name=published_subfolder_name,
            )
        except CanvaError as exc:
            if is_canva_auth_error(exc):
                raise
            attempts.append(f"canva catalog: {exc}")
        else:
            if canva_result is not None and canva_result.path is not None:
                return canva_result
            attempts.append("canva catalog: no thumbnail downloaded")

    if drive is None:
        attempts.append("tn generation: Google Drive client unavailable")
    else:
        try:
            generated = resolve_generated_tn_thumbnail(
                title=lookup_title,
                record_fields=record_fields,
                drive=drive,
                tn_settings=tn_settings,
            )
        except TnPublishError as exc:
            attempts.append(f"tn generation: {exc}")
        else:
            if generated.path is not None:
                return generated
            attempts.append("tn generation: render returned no file")

    detail = "; ".join(attempts) if attempts else "no resolution steps attempted"
    raise TnPublishError(
        f"Could not resolve translated thumbnail for {lookup_title!r} ({detail})"
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
    airtable: Any | None = None,
    record_id: str | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    if cleanup is None:
        return
    emit = log or (lambda message: None)

    if drive is not None:
        for move in cleanup.drive_file_moves:
            try:
                drive.move_file(move.file_id, move.destination_folder_id)
                emit(
                    "  cleanup: moved Drive override file "
                    f"{move.file_id} to Published folder"
                )
            except GoogleDriveError as exc:
                emit(
                    "  cleanup: failed to move Drive override file "
                    f"{move.file_id}: {exc}"
                )

        for file_id in cleanup.drive_file_ids_to_delete:
            try:
                drive.delete_file(file_id)
                emit(f"  cleanup: deleted Drive override file {file_id}")
            except GoogleDriveError as exc:
                emit(f"  cleanup: failed to delete Drive file {file_id}: {exc}")

        if cleanup.combined_media_file_id:
            try:
                action = drive.remove_file(cleanup.combined_media_file_id)
                emit(
                    "  cleanup: "
                    f"{action} Combined Media File {cleanup.combined_media_file_id}"
                )
                if airtable is not None and record_id:
                    airtable.update_record(
                        record_id,
                        {FIELD_COMBINED_MEDIA_FILE: ""},
                    )
                    emit(f"  cleanup: cleared {FIELD_COMBINED_MEDIA_FILE!r} on {record_id}")
            except GoogleDriveError as exc:
                emit(
                    "  cleanup: failed to remove Combined Media File "
                    f"{cleanup.combined_media_file_id}: {exc}"
                )

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
