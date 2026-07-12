"""Render English TN captions onto matched Drive template artboards."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_STATUS,
    FIELD_TYPE,
    FIELD_VIDEO_FOLDER,
    TYPE_QUOTE,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import original_thumbnail_destination
from media_publisher.sources.tn_docx import (
    TN_LABEL,
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

STATUS_KEYS = (
    "To do",
    "Translation done",
    "Editing done",
    "Synchronization done",
)
FOLDER_ID_RE = re.compile(r"(?:folders/|folder/)([a-zA-Z0-9_-]+)")
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".psd",
}
WORD_DOC_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DEFAULT_ORIGINAL_DIR = PROJECT_ROOT / "downloads" / "original-thumbnails"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "downloads" / "tn-cache"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "downloads" / "tn-rendered"
DEFAULT_OVERRIDE_FILE = PROJECT_ROOT / "downloads" / "tn-english-overrides.json"


def load_english_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return {str(key): str(value) for key, value in data.items()}


def english_override_for_title(overrides: dict[str, str], title: str) -> str | None:
    if title in overrides:
        return overrides[title]
    lowered = title.casefold()
    for key, value in overrides.items():
        if key.casefold() in lowered or lowered in key.casefold():
            return value
    return None


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


def status_bucket(status: object) -> str | None:
    if status is None:
        return None
    text = str(status)
    for key in STATUS_KEYS:
        if key.casefold() in text.casefold():
            return key
    return None


def is_image_file(name: str, mime_type: str) -> bool:
    if mime_type.startswith("image/"):
        return True
    if "photoshop" in mime_type.casefold():
        return True
    return Path(name).suffix.casefold() in IMAGE_EXTENSIONS


def build_filter_formula() -> str:
    clauses = [f'FIND("{key}", {{Status}} & "")' for key in STATUS_KEYS]
    type_clause = f'{{Type}} != "{TYPE_QUOTE}"'
    return f"AND(OR({', '.join(clauses)}), {type_clause})"


def render_destination(output_dir: Path, title: str) -> Path:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
    return output_dir / f"{cleaned or 'thumbnail'}.tn-render.jpg"


def find_english_text(drive: GoogleDriveClient, docs) -> str | None:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-dir", type=Path, default=DEFAULT_ORIGINAL_DIR)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--english-override-file",
        type=Path,
        default=DEFAULT_OVERRIDE_FILE,
        help="JSON map of catalog title -> English TN text for videos missing Drive docs",
    )
    args = parser.parse_args()

    english_overrides = load_english_overrides(args.english_override_file)

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    )
    records = airtable.list_records(filter_formula=build_filter_formula())

    folder_cache: dict[str, list] = {}
    targets: list[dict] = []

    for record in records:
        fields = record.fields
        bucket = status_bucket(fields.get(FIELD_STATUS))
        if bucket is None:
            continue
        folder_id = parse_folder_id(fields.get(FIELD_VIDEO_FOLDER))
        if folder_id is None:
            continue
        if folder_id not in folder_cache:
            folder_cache[folder_id] = drive.list_children(folder_id)
        children = folder_cache[folder_id]
        images = [item for item in children if is_image_file(item.name, item.mime_type)]
        if not images:
            continue
        docs = sorted(
            [
                item
                for item in children
                if item.mime_type in (WORD_DOC_MIME, GOOGLE_DOC_MIME)
            ],
            key=document_sort_key,
        )
        targets.append(
            {
                "title": catalog_title(fields),
                "status": bucket,
                "type": str(fields.get(FIELD_TYPE) or ""),
                "images": images,
                "docs": docs,
            }
        )

    print(f"=== Render TN thumbnails ({len(targets)} pilot videos) ===")
    print(f"Output dir: {args.output_dir.resolve()}")
    print()

    ok = 0
    skipped = 0
    failed: list[tuple[str, str]] = []

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        title = item["title"]
        destination = render_destination(args.output_dir, title)
        original_path = original_thumbnail_destination(args.original_dir, title)

        print(f"{title}")
        print(f"  status: {item['status']} | type: {item['type']}")

        if destination.exists() and not args.force:
            skipped += 1
            print(f"  result: SKIPPED ({destination.name})")
            print()
            continue

        if not original_path.exists():
            failed.append((title, "missing original thumbnail"))
            print("  result: FAILED (missing original thumbnail)")
            print()
            continue

        original_size = read_pillow_size(original_path)
        if original_size is None:
            failed.append((title, "unreadable original thumbnail"))
            print("  result: FAILED (unreadable original thumbnail)")
            print()
            continue

        english = find_english_text(drive, item["docs"])
        if not english:
            english = english_override_for_title(english_overrides, title)
        if not english:
            failed.append((title, "missing English TN text in Drive doc"))
            print("  result: FAILED (missing English TN text)")
            print()
            continue

        matched_layer: ImageSize | None = None
        matched_file = None
        cached_path: Path | None = None
        for child in sorted(item["images"], key=lambda row: row.name.casefold()):
            cache_path = args.cache_dir / safe_cache_name(child.name)
            if not cache_path.exists():
                drive.download_file(child.id, cache_path)
            candidates = collect_image_sizes(cache_path)
            matches = best_aspect_matches(original_size, candidates)
            if matches:
                matched_layer = matches[0]
                matched_file = child.name
                cached_path = cache_path
                break

        if matched_layer is None or cached_path is None:
            failed.append((title, "no Drive image with matching aspect ratio"))
            print("  result: FAILED (no matching aspect ratio in Drive TN file)")
            print()
            continue

        print(f"  original: {original_size.width}x{original_size.height}")
        print(f"  template: {matched_file} ({matched_layer.source})")
        print(f"  english:  {english[:100]}{'...' if len(english) > 100 else ''}")

        try:
            template, line_styles = load_template_image(cached_path, matched_layer)
            result = render_tn_thumbnail(
                template=template,
                english_text=english,
                line_styles=line_styles,
                destination=destination,
                catalog_title=title,
            )
        except (TnPsdError, TnRenderError, OSError) as exc:
            failed.append((title, str(exc)))
            print(f"  result: FAILED ({exc})")
            print()
            continue

        ok += 1
        print(
            f"  result: OK {result.width}x{result.height}, "
            f"{result.line_count} line(s), {len(line_styles)} template line slot(s)"
        )
        print(f"  saved:  {destination.name}")
        print()

    print("=== Summary ===")
    print(f"Rendered: {ok}")
    print(f"Skipped:  {skipped}")
    print(f"Failed:   {len(failed)}")
    if failed:
        print()
        for title, reason in failed:
            print(f"  - {title}: {reason}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
