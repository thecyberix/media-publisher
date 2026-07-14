"""Generate TN thumbnails from Airtable Original Video Thumbnail + translated caption."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import requests
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_VIDEO_CAPTION_TRANSLATED,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.tn_docx import caption_lines_for_render
from media_publisher.sources.tn_publish import render_destination
from media_publisher.sources.tn_reference import (
    cover_reference_text,
    extract_line_styles_from_reference_thumbnail,
)
from media_publisher.sources.tn_renderer import TnRenderError, render_tn_thumbnail

DEFAULT_TITLES = (
    "During A Satsang At The Mystic Musings Program Sadhguru Spoke About Solar Flares",
    "Meera Bais Bhakti Was Sweet And Crazy",
)


def download_airtable_thumbnail(fields: dict, destination: Path) -> None:
    attachment = fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL)
    if not isinstance(attachment, list) or not attachment:
        raise RuntimeError("Original Video Thumbnail is missing in Airtable")
    first = attachment[0]
    if not isinstance(first, dict):
        raise RuntimeError("Original Video Thumbnail attachment is invalid")
    url = str(first.get("url") or "").strip()
    if not url:
        raise RuntimeError("Original Video Thumbnail attachment has no URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    destination.write_bytes(response.content)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--title", action="append", dest="titles")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    needles = args.titles or list(DEFAULT_TITLES)

    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    output_dir = PROJECT_ROOT / "downloads" / "tn-rendered"
    staging_dir = PROJECT_ROOT / "downloads" / "original-thumbnails"
    output_dir.mkdir(parents=True, exist_ok=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    records = [
        record
        for record in airtable.list_records()
        if any(
            needle.casefold() in catalog_title(record.fields).casefold()
            for needle in needles
        )
    ]
    if not records:
        print("No matching records found.")
        return 1

    ok = 0
    failures: list[tuple[str, str]] = []
    for record in sorted(records, key=lambda item: catalog_title(item.fields).casefold()):
        fields = record.fields
        title = catalog_title(fields)
        destination = render_destination(output_dir, title)
        if destination.is_file() and args.force:
            destination.unlink()

        print(title)
        caption = fields.get(FIELD_VIDEO_CAPTION_TRANSLATED)
        if not isinstance(caption, str) or not caption.strip():
            failures.append((title, "missing Video caption translated in Airtable"))
            print("  FAIL missing translated caption")
            print()
            continue

        caption_lines = caption_lines_for_render(caption.strip())
        if not caption_lines:
            failures.append((title, "could not parse translated caption"))
            print("  FAIL could not parse translated caption")
            print()
            continue

        staging_path = staging_dir / f"{destination.stem}.original.jpg"
        try:
            download_airtable_thumbnail(fields, staging_path)
            with Image.open(staging_path) as image:
                template = image.convert("RGB")
        except (RuntimeError, OSError, requests.RequestException) as exc:
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
            print()
            continue

        caption_text = "\n".join(caption_lines)
        print(f"  template: {template.size[0]}x{template.size[1]}")
        print(f"  caption:  {' / '.join(caption_lines)}")

        reference = template.copy()
        line_styles = extract_line_styles_from_reference_thumbnail(
            reference,
            template.size,
            caption_line_count=len(caption_lines),
        )
        if not line_styles:
            failures.append((title, "could not derive line styles from original English text"))
            print("  FAIL could not derive line styles from original English text")
            print()
            continue

        template = cover_reference_text(reference)
        print(f"  styles:   {len(line_styles)} reference line(s) from original English layout")

        try:
            result = render_tn_thumbnail(
                template=template,
                english_text=caption_text,
                line_styles=line_styles,
                destination=destination,
                catalog_title=title,
            )
        except TnRenderError as exc:
            failures.append((title, str(exc)))
            print(f"  FAIL {exc}")
            print()
            continue

        ok += 1
        print(
            f"  OK {result.width}x{result.height}, {result.line_count} line(s) -> {destination.name}"
        )
        print()

    print(f"Rendered: {ok}/{len(records)}")
    if failures:
        for title, reason in failures:
            print(f"  - {title}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
