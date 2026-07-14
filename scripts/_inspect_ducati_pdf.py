"""Inspect Ducati PDF flatten and compare to original platform."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location(
    "audit_tn", PROJECT_ROOT / "scripts" / "audit_tn_and_canva.py"
)
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_tn"] = audit
spec.loader.exec_module(audit)

spec_cache = importlib.util.spec_from_file_location(
    "cache_pkgtn", PROJECT_ROOT / "scripts" / "cache_pkgtn_thumbnails.py"
)
cache_pkgtn = importlib.util.module_from_spec(spec_cache)
spec_cache.loader.exec_module(cache_pkgtn)

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.airtable_thumbnail import pick_root_thumbnail_marker
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.source_thumbnail import (
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)
from media_publisher.sources.tn_psd import safe_cache_name

OUT = PROJECT_ROOT / "downloads" / "drive-tn-uploads" / "_ducati-inspect"


def band_stats(image: Image.Image, y_start: float, y_end: float) -> dict:
    rgb = image.convert("RGB")
    w, h = rgb.size
    top = int(h * y_start)
    bottom = int(h * y_end)
    pixels = list(rgb.crop((0, top, w, bottom)).getdata())
    if not pixels:
        return {"mean_luma": 0, "variance": 0}
    lumas = [sum(px) / 3 for px in pixels]
    mean = sum(lumas) / len(lumas)
    var = sum((x - mean) ** 2 for x in lumas) / len(lumas)
    return {"mean_luma": mean, "variance": var}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    drive = GoogleDriveClient.from_service_account(
        PROJECT_ROOT / settings.google_sheets_service_account
    )
    original_dir = PROJECT_ROOT / settings.tn_original_thumbnail_dir

    record = next(
        item
        for item in airtable.list_records(filter_formula=audit.build_filter_formula())
        if "Ducati" in catalog_title(item.fields)
    )
    title = catalog_title(record.fields)
    source_url = str(record.fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
    folder_id = audit.parse_folder_id(record.fields.get("Video Folder"))
    marker = pick_root_thumbnail_marker(drive.list_children(folder_id))

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / safe_cache_name(marker.name)
        drive.download_file(marker.id, path)
        pdf_path = path.with_suffix(".pdf")
        if not pdf_path.exists():
            pdf_path.write_bytes(path.read_bytes())

        drive_image = cache_pkgtn.flatten_drive_file(pdf_path, mime_type="application/pdf")
        orig_path = original_thumbnail_destination(original_dir, title)
        fetch_original_video_thumbnail(source_url, orig_path)
        with Image.open(orig_path) as image:
            original = image.convert("RGB")

        drive_out = OUT / "drive-flatten.jpg"
        orig_out = OUT / "original-platform.jpg"
        drive_image.save(drive_out, quality=92)
        original.save(orig_out, quality=92)

        # Compare top text band region
        for label, img in ("drive", drive_image), ("original", original):
            top = band_stats(img, 0.0, 0.35)
            photo = band_stats(img, 0.35, 0.58)
            print(f"{label}: size={img.size}")
            print(f"  top band (0-35%): luma={top['mean_luma']:.1f} var={top['variance']:.1f}")
            print(f"  photo band:       luma={photo['mean_luma']:.1f} var={photo['variance']:.1f}")

        # Pixel diff in top 40% (where title text would be)
        resized_orig = original.resize(drive_image.size, Image.Resampling.LANCZOS)
        w, h = drive_image.size
        top_h = int(h * 0.4)
        d_top = drive_image.crop((0, 0, w, top_h))
        o_top = resized_orig.crop((0, 0, w, top_h))
        pa = list(d_top.getdata())
        pb = list(o_top.getdata())
        diff = sum(abs(a - b) for p1, p2 in zip(pa, pb, strict=False) for a, b in zip(p1, p2))
        top_sim = 1 - diff / (len(pa) * 3 * 255)
        print(f"Top-region similarity (drive vs original): {top_sim:.3f}")
        print(f"Saved: {drive_out}")
        print(f"Saved: {orig_out}")

    att = record.fields.get(FIELD_ORIGINAL_VIDEO_THUMBNAIL) or []
    if att:
        print("Airtable file:", att[0].get("filename"), att[0].get("url", "")[:100])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
