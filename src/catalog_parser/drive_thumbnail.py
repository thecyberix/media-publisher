from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from googleapiclient.discovery import Resource

from catalog_parser.canva import (
    CanvaClient,
    CanvaError,
    extract_canva_design_url,
    is_canva_auth_error,
)
from catalog_parser.canva_selection import (
    collect_canva_urls_from_values,
    dedupe_canva_urls,
    extract_canva_links_from_docx,
    extract_canva_links_from_google_document,
    select_canva_url,
)
from catalog_parser.drive_docs import (
    DEFAULT_YT_THUMBNAIL_FIELD,
    GOOGLE_DOC_MIME_TYPE,
    WORD_DOC_MIME_TYPE,
    drive_file_download_url,
    extract_drive_file_id,
    extract_drive_folder_id,
    list_text_documents_in_folder,
    read_drive_fields_from_folder,
)
from catalog_parser.drive_media import list_folder_children, resolve_drive_item
from catalog_parser.drive_video_size import video_size_from_pkg_folder

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
    if canva_client is None:
        raise CanvaError("Canva client is not configured")
    return resolve_thumbnail_from_canva_link(canva_client, canva_url), "canva-export"


def _canva_url_from_drive_fields(
    fields: dict[str, str | None],
    *,
    target_size: tuple[int, int] | None = None,
    original_video_url: str | None = None,
) -> str | None:
    field_urls = collect_canva_urls_from_values(list(fields.values()))
    return select_canva_url(
        field_urls,
        target_size=target_size,
        original_video_url=original_video_url,
    )


def _collect_canva_urls_from_folder_documents(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
) -> list[str]:
    import io

    from docx import Document

    all_urls: list[str] = []
    for document in list_text_documents_in_folder(drive_service, folder_id):
        document_id = document.get("id")
        mime_type = document.get("mimeType")
        if not isinstance(document_id, str) or not document_id:
            continue
        try:
            if mime_type == WORD_DOC_MIME_TYPE:
                content = drive_service.files().get_media(fileId=document_id).execute()
                docx_document = Document(io.BytesIO(content))
                canva_any, _canva_below_tn = extract_canva_links_from_docx(docx_document)
            elif mime_type == GOOGLE_DOC_MIME_TYPE and docs_service is not None:
                google_document = (
                    docs_service.documents().get(documentId=document_id).execute()
                )
                if not isinstance(google_document, dict):
                    continue
                canva_any, _canva_below_tn = extract_canva_links_from_google_document(
                    google_document
                )
            else:
                continue
        except Exception:
            continue
        all_urls.extend(canva_any)
    return dedupe_canva_urls(all_urls)


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
    *,
    original_video_url: str | None = None,
) -> str | None:
    all_urls = collect_canva_urls_from_values(list(fields.values()))
    all_urls.extend(
        _collect_canva_urls_from_folder_documents(
            drive_service,
            docs_service,
            folder_id,
        )
    )

    canva_url = select_canva_url(
        all_urls,
        target_size=video_size_from_pkg_folder(drive_service, folder_id),
        original_video_url=original_video_url,
    )
    if canva_url:
        return canva_url

    thumbnail_value = fields.get(DEFAULT_YT_THUMBNAIL_FIELD)
    if isinstance(thumbnail_value, str):
        file_id = extract_drive_file_id(thumbnail_value)
        if file_id:
            return _canva_url_from_drive_file_id(drive_service, file_id)
    return None


def resolve_canva_design_drive_url(
    drive_service: Any,
    record_fields: dict[str, Any],
    *,
    docs_service: Any | None = None,
) -> str | None:
    """Canva design URL from package docs in Video Folder, if one exists."""
    folder_value = record_fields.get("Video Folder")
    if folder_value is None:
        return None
    folder_id = extract_drive_folder_id(str(folder_value))
    if not folder_id:
        return None

    original = record_fields.get("Original Video")
    original_video_url = (
        original.strip() if isinstance(original, str) and original.strip() else None
    )

    try:
        urls = _collect_canva_urls_from_folder_documents(
            drive_service,
            docs_service,
            folder_id,
        )
        return select_canva_url(urls, original_video_url=original_video_url)
    except Exception:
        return None


def resolve_original_video_thumbnail(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
    *,
    original_video_url: str | None = None,
    canva_client: CanvaClient | None = None,
) -> tuple[list[dict[str, str]] | None, str | None]:
    """Resolve an Original Video Thumbnail attachment for ingest.

    Drive TN templates are ignored here (used later for offline translated TN render).
    Canva designs are preferred when present.
    """
    fields: dict[str, str | None]
    try:
        fields = read_drive_fields_from_folder(
            drive_service,
            docs_service,
            folder_id,
        )
    except Exception:
        fields = {}

    canva_url = _discover_canva_url(
        drive_service,
        docs_service,
        folder_id,
        fields,
        original_video_url=original_video_url,
    )
    if canva_url is None:
        return None, None

    try:
        return _resolve_canva_attachment(canva_url, canva_client=canva_client)
    except Exception as exc:
        raise DriveThumbnailError(str(exc)) from exc


def has_original_video_thumbnail_source(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
) -> bool:
    """True when a Canva design is available for direct Original Thumbnail ingest."""
    try:
        fields = read_drive_fields_from_folder(drive_service, docs_service, folder_id)
    except Exception:
        fields = {}
    return _discover_canva_url(drive_service, docs_service, folder_id, fields) is not None


def _download_http_url(url: str, destination: Path) -> None:
    import urllib.error
    import urllib.request

    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; media-publisher/1.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            destination.write_bytes(response.read())
    except urllib.error.URLError as exc:
        raise DriveThumbnailError(f"Failed to download thumbnail from {url!r}: {exc}") from exc


def download_canva_thumbnail(
    canva_url: str,
    destination: Path,
    *,
    canva_client: CanvaClient | None = None,
) -> str:
    """Write a Canva API design export to ``destination``; return source label."""
    attachment, source = _resolve_canva_attachment(canva_url, canva_client=canva_client)
    if not attachment:
        raise DriveThumbnailError(f"No Canva thumbnail attachment for {canva_url!r}")
    url = attachment[0].get("url")
    if not isinstance(url, str) or not url.strip():
        raise DriveThumbnailError(f"Canva thumbnail attachment missing URL for {canva_url!r}")
    _download_http_url(url.strip(), destination)
    return source


def _stage_manual_canva_review_placeholder(
    destination: Path,
    *,
    canva_url: str,
    drive_service: Resource,
    folder_id: str,
    updated: dict[str, Any],
    thumbnail_field: str,
) -> None:
    from media_publisher.sources.thumbnail_review import (
        write_manual_canva_review_placeholder,
    )

    size = video_size_from_pkg_folder(drive_service, folder_id)
    write_manual_canva_review_placeholder(
        destination,
        canva_url=canva_url,
        size=size,
    )
    updated["_thumbnailReviewPath"] = str(destination)
    updated.pop("_originalThumbnailPath", None)
    updated[thumbnail_field] = None
    updated[f"{thumbnail_field}Source"] = "canva-manual:review-queue"
    updated.pop(f"{thumbnail_field}Error", None)
    print(f"  -> staged manual Canva download placeholder for review: {destination.name}")


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


def find_peer_youtube_ct_link(
    folder_id: str,
    catalog_peers: list[dict[str, Any]],
    *,
    folder_link_field: str = "pkgLink",
    exclude_ct_link: str | None = None,
) -> str | None:
    """Find another catalog row with the same Drive folder and a YouTube ctLink."""
    from media_publisher.sources.source_thumbnail import detect_platform

    exclude = (exclude_ct_link or "").strip()
    for peer in catalog_peers:
        peer_folder_link = peer.get(folder_link_field)
        if not isinstance(peer_folder_link, str) or not peer_folder_link.strip():
            continue
        peer_folder_id = extract_drive_folder_id(peer_folder_link)
        if peer_folder_id != folder_id:
            continue
        peer_ct_link = str(peer.get("ctLink") or "").strip()
        if not peer_ct_link or peer_ct_link == exclude:
            continue
        if detect_platform(peer_ct_link) == "youtube":
            return peer_ct_link
    return None


def _original_thumbnail_matches_video_aspect(
    drive_service: Resource,
    folder_id: str,
    thumbnail_path: Path,
) -> bool:
    from media_publisher.sources.source_thumbnail import aspects_match
    from PIL import Image

    video_size = video_size_from_pkg_folder(drive_service, folder_id)
    if video_size is None:
        return False
    with Image.open(thumbnail_path) as image:
        width, height = image.size
    return aspects_match(width, height, video_size[0], video_size[1])


def _stage_original_thumbnail_for_ingest(
    drive_service: Resource,
    docs_service: Resource | None,
    folder_id: str,
    *,
    source_url: str,
    destination: Path,
    updated: dict[str, Any],
    thumbnail_field: str,
    canva_client: CanvaClient | None = None,
) -> None:
    """Stage original thumbnail for Airtable upload or Drive review.

    Priority:
    1. Canva link → Canva API design export (direct Airtable upload)
    2. Canva link + design-level API failure → manual-download placeholder for review
    3. Otherwise matching-aspect original-platform thumbs are queued for review

    Canva auth failures raise (workflow should fail). Drive TN templates are ignored
    at ingest (used later for offline translated TN render).
    """
    updated[thumbnail_field] = None
    updated.pop(f"{thumbnail_field}Error", None)

    fields: dict[str, str | None]
    try:
        fields = read_drive_fields_from_folder(drive_service, docs_service, folder_id)
    except Exception:
        fields = {}
    canva_url = _discover_canva_url(
        drive_service,
        docs_service,
        folder_id,
        fields,
        original_video_url=source_url,
    )
    if canva_url is not None:
        try:
            source = download_canva_thumbnail(
                canva_url,
                destination,
                canva_client=canva_client,
            )
        except Exception as exc:
            if is_canva_auth_error(exc):
                raise DriveThumbnailError(str(exc)) from exc
            print(f"  -> Canva API export failed (manual review): {exc}")
            _stage_manual_canva_review_placeholder(
                destination,
                canva_url=canva_url,
                drive_service=drive_service,
                folder_id=folder_id,
                updated=updated,
                thumbnail_field=thumbnail_field,
            )
            return
        updated["_originalThumbnailPath"] = str(destination)
        updated.pop("_thumbnailReviewPath", None)
        updated[f"{thumbnail_field}Source"] = source
        print(f"  -> staged Canva design for Airtable upload: {destination.name}")
        return

    download_original_platform_thumbnail(source_url, destination)
    if _original_thumbnail_matches_video_aspect(drive_service, folder_id, destination):
        updated["_thumbnailReviewPath"] = str(destination)
        updated.pop("_originalThumbnailPath", None)
        updated[f"{thumbnail_field}Source"] = "original-platform:review-queue"
        print(f"  -> staged for review queue: {destination.name}")
        return

    destination.unlink(missing_ok=True)
    updated.pop("_originalThumbnailPath", None)
    updated.pop("_thumbnailReviewPath", None)
    updated[f"{thumbnail_field}Source"] = None
    print("  -> no thumbnail source (skipped review: aspect mismatch)")


def enrich_records_with_original_video_thumbnails(
    records: list[dict[str, Any]],
    drive_service: Resource,
    docs_service: Resource | None,
    *,
    canva_client: CanvaClient | None = None,
    folder_link_field: str = "pkgLink",
    thumbnail_field: str = DEFAULT_YT_THUMBNAIL_FIELD,
    staging_dir: Path | None = None,
    catalog_peers: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(records)
    peers = catalog_peers if catalog_peers is not None else records

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
            source_url = str(updated.get("ctLink") or "").strip()
            if staging_dir is not None:
                if not source_url:
                    raise DriveThumbnailError(
                        "Cannot stage original thumbnail on ingest: ctLink is missing"
                    )
                from media_publisher.sources.source_thumbnail import (
                    detect_platform,
                    original_thumbnail_destination,
                )

                destination = original_thumbnail_destination(
                    staging_dir,
                    label_text if isinstance(label, str) and label else f"row-{index}",
                )
                try:
                    _stage_original_thumbnail_for_ingest(
                        drive_service,
                        docs_service,
                        folder_id,
                        source_url=source_url,
                        destination=destination,
                        updated=updated,
                        thumbnail_field=thumbnail_field,
                        canva_client=canva_client,
                    )
                except DriveThumbnailError as primary_exc:
                    if is_canva_auth_error(primary_exc):
                        raise
                    peer_yt = None
                    if detect_platform(source_url) == "instagram":
                        peer_yt = find_peer_youtube_ct_link(
                            folder_id,
                            peers,
                            folder_link_field=folder_link_field,
                            exclude_ct_link=source_url,
                        )
                    if not peer_yt:
                        raise
                    print(
                        "  -> IG thumbnail failed; "
                        f"trying peer YouTube link ({peer_yt})"
                    )
                    destination.unlink(missing_ok=True)
                    _stage_original_thumbnail_for_ingest(
                        drive_service,
                        docs_service,
                        folder_id,
                        source_url=peer_yt,
                        destination=destination,
                        updated=updated,
                        thumbnail_field=thumbnail_field,
                        canva_client=canva_client,
                    )
                    updated["_originalThumbnailFallbackCtLink"] = peer_yt
                    updated.pop(f"{thumbnail_field}Error", None)
                    source_label = updated.get(f"{thumbnail_field}Source")
                    if isinstance(source_label, str) and source_label:
                        updated[f"{thumbnail_field}Source"] = (
                            f"{source_label}:peer-youtube"
                        )
                    print(f"  -> used peer YouTube after IG failure: {primary_exc}")
            elif has_original_video_thumbnail_source(
                drive_service,
                docs_service,
                folder_id,
            ):
                if not source_url:
                    raise DriveThumbnailError(
                        "Thumbnail source found in Drive but ctLink is missing"
                    )
                attachment, source = resolve_original_video_thumbnail(
                    drive_service,
                    docs_service,
                    folder_id,
                    original_video_url=source_url,
                    canva_client=canva_client,
                )
                updated[thumbnail_field] = attachment
                updated[f"{thumbnail_field}Source"] = source
                updated.pop(f"{thumbnail_field}Error", None)
                if attachment:
                    print(f"  -> {thumbnail_field}: {source}")
                else:
                    print("  -> not found")
            else:
                updated[thumbnail_field] = None
                updated[f"{thumbnail_field}Source"] = None
                updated.pop(f"{thumbnail_field}Error", None)
                print("  -> no thumbnail source")
        except DriveThumbnailError as exc:
            if is_canva_auth_error(exc):
                raise
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = str(exc)
            print(f"  -> error: {exc}")
        except Exception as exc:
            if is_canva_auth_error(exc):
                raise
            updated[thumbnail_field] = None
            updated[f"{thumbnail_field}Source"] = None
            updated[f"{thumbnail_field}Error"] = str(exc)
            print(f"  -> error: {exc}")

        enriched.append(updated)

    return enriched
