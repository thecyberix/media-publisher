"""Resolve Airtable Original Video Thumbnail attachments from Drive assets."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from media_publisher.sources.airtable import build_airtable_attachment
from media_publisher.sources.canva import (
    CanvaClient,
    CanvaError,
    parse_design_id,
    resolve_canva_url,
)
from media_publisher.sources.source_thumbnail import SourceThumbnailError, extract_thumbnail_url

PDF_MIME = "application/pdf"
ROOT_MARKER_EXTENSIONS = {".jpg", ".jpeg", ".psd", ".pdf"}


class DriveChild(Protocol):
    name: str
    mime_type: str


def is_root_thumbnail_marker(name: str, mime_type: str) -> bool:
    suffix = Path(name).suffix.casefold()
    if suffix in ROOT_MARKER_EXTENSIONS:
        return True
    if mime_type == PDF_MIME:
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return False


def pick_root_thumbnail_marker(children) -> object | None:
    candidates = [
        child
        for child in children
        if is_root_thumbnail_marker(child.name, child.mime_type)
    ]
    if not candidates:
        return None

    def rank(child) -> tuple[int, str]:
        name = child.name.casefold()
        if name.startswith("tn_"):
            return (0, name)
        if "thumb" in name:
            return (1, name)
        if name.endswith(".pdf"):
            return (2, name)
        return (3, name)

    return sorted(candidates, key=rank)[0]


def resolve_original_platform_attachment(source_url: str) -> tuple[list[dict[str, str]], str]:
    thumbnail_url, platform, method = extract_thumbnail_url(source_url)
    filename = f"original-{platform}.jpg"
    return build_airtable_attachment(thumbnail_url, filename=filename), method


def resolve_canva_attachment(
    canva_url: str,
    *,
    canva_client: CanvaClient | None,
) -> tuple[list[dict[str, str]], str]:
    if canva_client is None:
        raise CanvaError("Canva client is not configured")
    resolved = resolve_canva_url(canva_url)
    design_id = parse_design_id(resolved)
    if design_id is None:
        raise CanvaError(f"Could not parse Canva design id from {canva_url!r}")
    job = canva_client.export_design(design_id, export_format="jpg")
    if not job.urls:
        raise CanvaError("Canva export returned no download URLs")
    return (
        build_airtable_attachment(job.urls[0], filename="canva-thumbnail.jpg"),
        "canva-export",
    )


def resolve_thumbnail_attachment(
    *,
    children,
    original_video_url: str | None,
    canva_url: str | None,
    canva_client: CanvaClient | None,
) -> tuple[list[dict[str, str]], str, str]:
    root_file = pick_root_thumbnail_marker(children)
    if root_file is not None:
        source_url = str(original_video_url or "").strip()
        if not source_url:
            raise RuntimeError(
                f"root thumbnail marker {root_file.name!r} but missing Original Video link"
            )
        attachment, method = resolve_original_platform_attachment(source_url)
        return attachment, "original-platform", f"{root_file.name} -> {method}"

    if not canva_url:
        raise RuntimeError("no root JPG/PSD/PDF and no Canva link in TEXT_ doc")

    attachment, method = resolve_canva_attachment(canva_url, canva_client=canva_client)
    return attachment, "canva", f"{canva_url} -> {method}"
