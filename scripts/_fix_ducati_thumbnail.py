"""Fix Ducati empty PDF template -> original platform thumbnail."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location(
    "upload_script",
    PROJECT_ROOT / "scripts" / "upload_drive_tn_thumbnails_to_airtable.py",
)
upload_script = importlib.util.module_from_spec(spec)
sys.modules["upload_script"] = upload_script
spec.loader.exec_module(upload_script)

spec = importlib.util.spec_from_file_location(
    "audit_tn", PROJECT_ROOT / "scripts" / "audit_tn_and_canva.py"
)
audit = importlib.util.module_from_spec(spec)
sys.modules["audit_tn"] = audit
spec.loader.exec_module(audit)

from media_publisher.config import load_settings
from media_publisher.sources.airtable import (
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    AirtableClient,
    catalog_title,
)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings(PROJECT_ROOT)
    airtable = AirtableClient(
        settings.airtable_token,
        settings.airtable_base_id,
        settings.airtable_table_name,
    )
    original_dir = PROJECT_ROOT / settings.tn_original_thumbnail_dir
    export_dir = PROJECT_ROOT / "downloads" / "drive-tn-uploads"
    export_dir.mkdir(parents=True, exist_ok=True)

    record = next(
        item
        for item in airtable.list_records(filter_formula=audit.build_filter_formula())
        if "Ducati" in catalog_title(item.fields)
    )
    title = catalog_title(record.fields)
    source_url = str(record.fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
    original_image = upload_script.load_original_platform_image(
        title=title,
        source_url=source_url,
        original_dir=original_dir,
    )
    if original_image is None:
        print("Could not load original-platform thumbnail")
        return 1

    export_path = export_dir / f"{upload_script.sanitize_filename(title)}.original-platform.jpg"
    upload_script.save_upload_jpeg(original_image, export_path)
    airtable.upload_attachment(
        record.id,
        FIELD_ORIGINAL_VIDEO_THUMBNAIL,
        export_path,
    )
    print(f"OK uploaded original-platform thumbnail for: {title}")
    print(f"File: {export_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
