"""Generate TN thumbnails at publish time for catalog videos."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import (
    SourceThumbnailError,
    original_thumbnail_destination,
    video_size_from_source_url,
)
from media_publisher.sources.tn_docx import caption_lines_for_render
from media_publisher.sources.tn_psd import (
    ImageSize,
    TnPsdError,
    best_aspect_matches,
    collect_image_sizes,
    load_template_image,
    read_pillow_size,
    safe_cache_name,
)
from media_publisher.sources.tn_renderer import TnRenderError, render_tn_thumbnail

FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd",
}


class TnPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class TnPublishSettings:
    original_dir: Path
    cache_dir: Path
    output_dir: Path
    english_override_file: Path


def parse_folder_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    match = FOLDER_ID_RE.search(text)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{10,}", text):
        return text
    return None


def render_destination(output_dir: Path, title: str) -> Path:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
    return output_dir / f"{cleaned or 'thumbnail'}.tn-render.jpg"


def _is_image_file(name: str, mime_type: str) -> bool:
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def _tn_template_sort_key(row: object) -> tuple[int, str]:
    name = getattr(row, "name", "").casefold()
    return (0 if name.endswith(".psd") else 1, name)


def find_tn_template_in_folder(
    drive: GoogleDriveClient,
    folder_id: str,
):
    """Return a root-level TN template image in ``folder_id``, preferring PSDs."""
    children = drive.list_children(folder_id)
    images = [item for item in children if _is_image_file(item.name, item.mime_type)]
    if not images:
        return None
    return sorted(images, key=_tn_template_sort_key)[0]


def resolve_tn_template_drive_url(
    drive_service: Any,
    record_fields: dict[str, Any],
) -> str | None:
    """Drive file view URL for a TN template in Video Folder, if one exists."""
    folder_id = parse_folder_id(record_fields.get(FIELD_VIDEO_FOLDER))
    if folder_id is None:
        return None
    try:
        drive = GoogleDriveClient(drive_service)
        template = find_tn_template_in_folder(drive, folder_id)
    except Exception:
        return None
    if template is None:
        return None
    from catalog_parser.drive_docs import drive_file_view_url

    return drive_file_view_url(template.id)


def _translated_caption_lines(record_fields: dict[str, Any]) -> list[str] | None:
    caption_translated = record_fields.get(FIELD_VIDEO_CAPTION_TRANSLATED)
    if not isinstance(caption_translated, str) or not caption_translated.strip():
        return None
    lines = caption_lines_for_render(caption_translated.strip())
    return lines or None


def reference_thumbnail_size(
    record_fields: dict[str, Any],
    *,
    title: str,
    original_dir: Path,
    drive: GoogleDriveClient | None = None,
    folder_id: str | None = None,
) -> ImageSize | None:
    """Return the aspect-ratio reference used when selecting TN templates."""
    resolved_folder_id = folder_id or parse_folder_id(record_fields.get(FIELD_VIDEO_FOLDER))
    if drive is not None and resolved_folder_id:
        from catalog_parser.drive_video_size import video_size_from_pkg_folder

        video_size = video_size_from_pkg_folder(drive.drive_service, resolved_folder_id)
        if video_size is not None:
            return ImageSize(
                width=video_size[0],
                height=video_size[1],
                source="drive-video",
            )

    source_url = record_fields.get(FIELD_ORIGINAL_VIDEO)
    if isinstance(source_url, str) and source_url.strip():
        try:
            video_size = video_size_from_source_url(source_url.strip())
        except SourceThumbnailError:
            video_size = None
        if video_size is not None:
            return ImageSize(
                width=video_size[0],
                height=video_size[1],
                source="original-video",
            )

    attachment = record_fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    if isinstance(attachment, list) and attachment:
        first = attachment[0]
        if isinstance(first, dict):
            width = first.get("width")
            height = first.get("height")
            if (
                isinstance(width, (int, float))
                and isinstance(height, (int, float))
                and width > 0
                and height > 0
            ):
                return ImageSize(
                    width=int(width),
                    height=int(height),
                    source="airtable-thumbnail",
                )

    original_path = original_thumbnail_destination(original_dir, title)
    return read_pillow_size(original_path)


def generate_catalog_tn_thumbnail(
    *,
    title: str,
    record_fields: dict[str, Any],
    drive: GoogleDriveClient,
    settings: TnPublishSettings,
) -> Path:
    """Render a TN thumbnail from Drive template + Airtable translated caption only."""
    destination = render_destination(settings.output_dir, title)
    if destination.is_file():
        return destination

    caption_lines = _translated_caption_lines(record_fields)
    if caption_lines is None:
        raise TnPublishError(f"Missing translated TN caption in Airtable for {title!r}")

    folder_id = parse_folder_id(record_fields.get(FIELD_VIDEO_FOLDER))
    if folder_id is None:
        raise TnPublishError(f"Missing Video Folder for {title!r}")

    reference_size = reference_thumbnail_size(
        record_fields,
        title=title,
        original_dir=settings.original_dir,
        drive=drive,
        folder_id=folder_id,
    )
    if reference_size is None:
        raise TnPublishError(f"Missing reference aspect ratio for {title!r}")

    children = drive.list_children(folder_id)
    images = [item for item in children if _is_image_file(item.name, item.mime_type)]
    if not images:
        raise TnPublishError(f"No TN template images in Drive folder for {title!r}")

    matched_layer: ImageSize | None = None
    cached_path: Path | None = None

    for child in sorted(images, key=_tn_template_sort_key):
        cache_path = settings.cache_dir / safe_cache_name(child.name)
        if not cache_path.exists():
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            drive.download_file(child.id, cache_path)
        matches = best_aspect_matches(reference_size, collect_image_sizes(cache_path))
        if matches:
            matched_layer = matches[0]
            cached_path = cache_path
            break

    if matched_layer is None or cached_path is None:
        raise TnPublishError(
            f"No Drive TN template with matching aspect ratio for {title!r}"
        )

    caption_text = "\n".join(caption_lines)
    try:
        template, line_styles = load_template_image(cached_path, matched_layer)
        render_tn_thumbnail(
            template=template,
            english_text=caption_text,
            line_styles=line_styles,
            destination=destination,
            catalog_title=title,
        )
    except (TnPsdError, TnRenderError, OSError) as exc:
        raise TnPublishError(str(exc)) from exc

    if not destination.is_file():
        raise TnPublishError(f"TN render did not create {destination}")
    return destination


def catalog_title_from_fields(record_fields: dict[str, Any]) -> str:
    return catalog_title(record_fields)
