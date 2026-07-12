from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from googleapiclient.discovery import Resource

from catalog_parser.canva import CanvaClient, CanvaError, extract_canva_design_url
from catalog_parser.drive_docs import (
    DEFAULT_YT_THUMBNAIL_FIELD,
    drive_file_download_url,
    extract_drive_file_id,
    extract_drive_folder_id,
    list_text_documents_in_folder,
    read_drive_fields_from_file,
    read_drive_fields_from_folder,
)
from catalog_parser.drive_media import list_folder_children, resolve_drive_item

IMAGE_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
)
THUMBNAIL_NAME_PATTERN = re.compile(r"thumb(?:nail)?", re.IGNORECASE)
PDF_MIME = "application/pdf"
ROOT_MARKER_EXTENSIONS = {".jpg", ".jpeg", ".psd", ".pdf"}


class DriveThumbnailError(RuntimeError):
    pass


def is_image_mime_type(mime_type: str) -> bool:
    return mime_type in IMAGE_MIME_TYPES or mime_type.startswith("image/")


def is_root_thumbnail_marker(name: str, mime_type: str) -> bool:
    suffix = Path(name).suffix.casefold()
    if suffix in ROOT_MARKER_EXTENSIONS:
        return True
    if mime_type == PDF_MIME:
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return False


def _thumbnail_name_rank(name: str) -> tuple[int, str]:
    normalized = name.casefold()
    if THUMBNAIL_NAME_PATTERN.search(normalized):
        return (0, normalized)
    return (1, normalized)


def find_thumbnail_image_in_folder(
    drive_service: Resource,
    folder_id: str,
) -> dict[str, str] | None:
    images: list[dict[str, str]] = []
    for item in list_folder_children(drive_service, folder_id):
        resolved = resolve_drive_item(drive_service, item)
        mime_type = resolved.get("mimeType")
        file_id = resolved.get("id")
        file_name = resolved.get("name")
        if not isinstance(mime_type, str) or not is_image_mime_type(mime_type):
            continue
        if not isinstance(file_id, str) or not file_id:
            continue
        images.append(
            {
                "id": file_id,
                "name": file_name if isinstance(file_name, str) else "",
                "mimeType": mime_type,
            }
        )

    if not images:
        return None

    images.sort(key=lambda item: _thumbnail_name_rank(item.get("name", "")))
    return images[0]


def pick_root_thumbnail_marker(
    drive_service: Resource,
    folder_id: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for item in list_folder_children(drive_service, folder_id):
        resolved = resolve_drive_item(drive_service, item)
        name = resolved.get("name")
        mime_type = resolved.get("mimeType")
        if not isinstance(name, str) or not isinstance(mime_type, str):
            continue
        if not is_root_thumbnail_marker(name, mime_type):
            continue
        candidates.append(resolved)

    if not candidates:
        return None

    def rank(item: dict[str, Any]) -> tuple[int, str]:
        name = str(item.get("name", "")).casefold()
        if name.startswith("tn_"):
            return (0, name)
        if "thumb" in name:
            return (1, name)
        if name.endswith(".pdf"):
            return (2, name)
        return (3, name)

    return sorted(candidates, key=rank)[0]


def build_airtable_attachment(url: str, *, filename: str | None = None) -> list[dict[str, str]]:
    attachment: dict[str, str] = {"url": url.strip()}
    if filename and filename.strip():
        attachment["filename"] = filename.strip()
    return [attachment]


def resolve_thumbnail_from_drive_image(
    drive_service: Resource,
    folder_id: str,
) -> list[dict[str, str]] | None:
    image = find_thumbnail_image_in_folder(drive_service, folder_id)
    if image is None:
        return None
    return build_airtable_attachment(
        drive_file_download_url(image["id"]),
        filename=image.get("name") or None,
    )


def resolve_thumbnail_from_canva_link(
    canva_client: CanvaClient,
    canva_url: str,
) -> list[dict[str, str]]:
    export_url = canva_client.export_design_url_from_link(canva_url)
    return build_airtable_attachment(export_url)


def _resolve_original_platform_attachment(source_url: str) -> tuple[list[dict[str, str]], str]:
    from media_publisher.sources.source_thumbnail import extract_thumbnail_url

    thumbnail_url, platform, method = extract_thumbnail_url(source_url)
    return (
        build_airtable_attachment(
            thumbnail_url,
            filename=f"original-{platform}.jpg",
        ),
        f"original-platform:{method}",
    )


def _resolve_canva_attachment(
    canva_url: str,
    *,
    canva_client: CanvaClient | None,
) -> tuple[list[dict[str, str]], str]:
    if canva_client is not None:
        try:
            return resolve_thumbnail_from_canva_link(canva_client, canva_url), "canva-export"
        except CanvaError:
            pass

    from media_publisher.sources.canva_share_preview import resolve_canva_share_preview_url

    preview_url = resolve_canva_share_preview_url(canva_url)
    return (
        build_airtable_attachment(preview_url, filename="canva-preview.jpg"),
        "canva-share-preview",
    )


def _canva_url_from_drive_fields(fields: dict[str, str | None]) -> str | None:
    thumbnail_value = fields.get(DEFAULT_YT_THUMBNAIL_FIELD)
    if isinstance(thumbnail_value, str):
        canva_url = extract_canva_design_url(thumbnail_value)
        if canva_url:
            return canva_url

    for value in fields.values():
        if not isinstance(value, str):
            continue
        canva_url = extract_canva_design_url(value)
        if canva_url:
            return canva_url
    return None


def _canva_url_from_drive_file_id(
    drive_service: Resource,
    file_id: str,
) -> str | None:
    file_id = file_id.strip()
    if not file_id:
        return None
    metadata = (
        drive_service.files()
        .get(
            fileId=file_id,
            fields="id,name,mimeType,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    if not isinstance(metadata, dict):
        return None
    for candidate in (
        metadata.get("webViewLink"),
        metadata.get("name"),
    ):
        if isinstance(candidate, str):
            canva_url = extract_canva_design_url(candidate)
            if canva_url:
                return canva_url
    return None


def _discover_canva_url(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
    fields: dict[str, str | None],
) -> str | None:
    canva_url = _canva_url_from_drive_fields(fields)
    if canva_url:
        return canva_url

    for document in list_text_documents_in_folder(drive_service, folder_id):
        try:
            doc_fields = read_drive_fields_from_file(
                drive_service,
                docs_service,
                document,
            )
        except Exception:
            continue
        canva_url = _canva_url_from_drive_fields(doc_fields)
        if canva_url:
            return canva_url

    thumbnail_value = fields.get(DEFAULT_YT_THUMBNAIL_FIELD)
    if isinstance(thumbnail_value, str):
        file_id = extract_drive_file_id(thumbnail_value)
        if file_id:
            canva_url = _canva_url_from_drive_file_id(drive_service, file_id)
            if canva_url:
                return canva_url
    return None


def resolve_original_video_thumbnail(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
    *,
    original_video_url: str | None = None,
    canva_client: CanvaClient | None = None,
) -> tuple[list[dict[str, str]] | None, str | None]:
    root_file = pick_root_thumbnail_marker(drive_service, folder_id)
    if root_file is not None:
        source_url = str(original_video_url or "").strip()
        if not source_url:
            raise DriveThumbnailError(
                f"Found root thumbnail marker {root_file.get('name')!r} "
                "but no Original Video URL (ctLink) was provided"
            )
        try:
            return _resolve_original_platform_attachment(source_url)
        except Exception as exc:
            raise DriveThumbnailError(str(exc)) from exc

    fields: dict[str, str | None]
    try:
        fields = read_drive_fields_from_folder(
            drive_service,
            docs_service,
            folder_id,
        )
    except Exception:
        fields = {}

    canva_url = _discover_canva_url(drive_service, docs_service, folder_id, fields)
    if canva_url is None:
        return None, None

    source_url = str(original_video_url or "").strip()
    if not source_url:
        raise DriveThumbnailError(
            f"Found Canva link {canva_url!r} but no Original Video URL (ctLink) was provided"
        )
    try:
        return _resolve_original_platform_attachment(source_url)
    except Exception as exc:
        raise DriveThumbnailError(str(exc)) from exc


def has_original_video_thumbnail_source(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
) -> bool:
    if pick_root_thumbnail_marker(drive_service, folder_id) is not None:
        return True
    try:
        fields = read_drive_fields_from_folder(drive_service, docs_service, folder_id)
    except Exception:
        fields = {}
    return _discover_canva_url(drive_service, docs_service, folder_id, fields) is not None


def download_original_platform_thumbnail(
    source_url: str,
    destination: Path,
) -> Path:
    from media_publisher.sources.source_thumbnail import (
        SourceThumbnailError,
        fetch_original_video_thumbnail,
    )

    try:
        fetch_original_video_thumbnail(source_url, destination)
    except SourceThumbnailError as exc:
        raise DriveThumbnailError(str(exc)) from exc
    return destination


def enrich_records_with_original_video_thumbnails(
    records: list[dict[str, Any]],
    drive_service: Resource,
    docs_service: Resource | None,
    *,
    canva_client: CanvaClient | None = None,
    folder_link_field: str = "pkgLink",
    thumbnail_field: str = DEFAULT_YT_THUMBNAIL_FIELD,
    staging_dir: Path | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(records)

    for index, record in enumerate(records, start=1):
        updated = dict(record)
        folder_link = updated.get(folder_link_field)
        label = updated.get("ctTitle")
        label_text = label if isinstance(label, str) and label else f"row {index}"
        print(f"Thumbnail {index}/{total}: {label_text}")

        if not isinstance(folder_link, str) or not folder_link.strip():
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = f"Missing {folder_link_field}"
            enriched.append(updated)
            print("  -> missing pkgLink")
            continue

        folder_id = extract_drive_folder_id(folder_link)
        if folder_id is None:
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = (
                f"Could not parse Drive folder id from {folder_link!r}"
            )
            enriched.append(updated)
            print("  -> invalid pkgLink")
            continue

        try:
            if staging_dir is not None and has_original_video_thumbnail_source(
                drive_service,
                docs_service,
                folder_id,
            ):
                source_url = str(updated.get("ctLink") or "").strip()
                if not source_url:
                    raise DriveThumbnailError(
                        "Thumbnail source found in Drive but ctLink is missing"
                    )
                from media_publisher.sources.source_thumbnail import original_thumbnail_destination

                destination = original_thumbnail_destination(
                    staging_dir,
                    label_text if isinstance(label, str) and label else f"row-{index}",
                )
                download_original_platform_thumbnail(source_url, destination)
                updated["_originalThumbnailPath"] = str(destination)
                updated[thumbnail_field] = None
                updated[f"{thumbnail_field}Source"] = "original-platform:local-upload"
                updated.pop(f"{thumbnail_field}Error", None)
                print(f"  -> staged for upload: {destination.name}")
            else:
                attachment, source = resolve_original_video_thumbnail(
                    drive_service,
                    docs_service,
                    folder_id,
                    original_video_url=updated.get("ctLink"),
                    canva_client=canva_client,
                )
                updated[thumbnail_field] = attachment
                updated[f"{thumbnail_field}Source"] = source
                updated.pop(f"{thumbnail_field}Error", None)
                if attachment:
                    print(f"  -> {thumbnail_field}: {source}")
                else:
                    print("  -> not found")
        except DriveThumbnailError as exc:
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = str(exc)
            print(f"  -> error: {exc}")
        except Exception as exc:
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = str(exc)
            print(f"  -> error: {exc}")

        enriched.append(updated)

    return enriched
