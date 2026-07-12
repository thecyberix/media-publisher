"""Generate TN thumbnails at publish time for catalog videos."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from media_publisher.sources.airtable import (
    FIELD_VIDEO_CAPTION_TRANSLATED,
    FIELD_VIDEO_FOLDER,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import original_thumbnail_destination
from media_publisher.sources.tn_docx import (
    TN_LABEL,
    caption_lines_for_render,
    document_sort_key,
    extract_labeled_table,
    extract_tn_text,
    read_word_document,
)
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
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


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


def load_english_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TnPublishError(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in data.items()}


def english_override_for_title(overrides: dict[str, str], title: str) -> str | None:
    if title in overrides:
        return overrides[title]
    lowered = title.casefold()
    for key, value in overrides.items():
        if key.casefold() in lowered or lowered in key.casefold():
            return value
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


def _find_english_text(drive: GoogleDriveClient, docs) -> str | None:
    for doc in docs:
        document = read_word_document(drive, doc)
        if document is None:
            continue
        grid = extract_labeled_table(document, TN_LABEL)
        if grid is None:
            continue
        values = extract_tn_text(grid)
        english = values.get("english")
        if english:
            return english
    return None


def generate_catalog_tn_thumbnail(
    *,
    title: str,
    record_fields: dict[str, Any],
    drive: GoogleDriveClient,
    settings: TnPublishSettings,
) -> Path:
    """Render a TN thumbnail JPG for a catalog video title."""
    destination = render_destination(settings.output_dir, title)
    if destination.is_file():
        return destination

    original_path = original_thumbnail_destination(settings.original_dir, title)
    if not original_path.is_file():
        raise TnPublishError(
            f"Missing original thumbnail for {title!r} at {original_path}"
        )

    original_size = read_pillow_size(original_path)
    if original_size is None:
        raise TnPublishError(f"Unreadable original thumbnail for {title!r}")

    caption_translated = record_fields.get(FIELD_VIDEO_CAPTION_TRANSLATED)
    if isinstance(caption_translated, str) and caption_translated.strip():
        english = "\n".join(caption_lines_for_render(caption_translated.strip()))
    else:
        folder_id = parse_folder_id(record_fields.get(FIELD_VIDEO_FOLDER))
        english = None
        if folder_id is not None:
            children = drive.list_children(folder_id)
            docs = sorted(
                [
                    item
                    for item in children
                    if item.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
                ],
                key=document_sort_key,
            )
            english = _find_english_text(drive, docs)
        if not english:
            overrides = load_english_overrides(settings.english_override_file)
            english = english_override_for_title(overrides, title)
    if not english:
        raise TnPublishError(f"Missing TN caption text for {title!r}")

    folder_id = parse_folder_id(record_fields.get(FIELD_VIDEO_FOLDER))
    if folder_id is None:
        raise TnPublishError(f"Missing Video Folder for {title!r}")

    children = drive.list_children(folder_id)
    images = [item for item in children if _is_image_file(item.name, item.mime_type)]
    if not images:
        raise TnPublishError(f"No TN template images in Drive folder for {title!r}")

    matched_layer: ImageSize | None = None
    cached_path: Path | None = None

    def template_sort_key(row) -> tuple[int, str]:
        name = row.name.casefold()
        return (0 if name.endswith(".psd") else 1, name)

    for child in sorted(images, key=template_sort_key):
        cache_path = settings.cache_dir / safe_cache_name(child.name)
        if not cache_path.exists():
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            drive.download_file(child.id, cache_path)
        candidates = collect_image_sizes(cache_path)
        matches = best_aspect_matches(original_size, candidates)
        if matches:
            matched_layer = matches[0]
            cached_path = cache_path
            break

    if matched_layer is None or cached_path is None:
        raise TnPublishError(
            f"No Drive TN template with matching aspect ratio for {title!r}"
        )

    try:
        template, line_styles = load_template_image(cached_path, matched_layer)
        render_tn_thumbnail(
            template=template,
            english_text=english,
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
