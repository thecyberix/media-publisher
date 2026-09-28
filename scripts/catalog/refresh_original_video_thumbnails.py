"""Re-export Original Video Thumbnails from Drive/Canva and replace Airtable files."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from catalog_parser.__main__ import (  # noqa: E402
    DEFAULT_CREDENTIALS,
    DEFAULT_TOKEN,
    PROJECT_ROOT,
    load_env_file,
)

load_env_file(PROJECT_ROOT / ".env")

from catalog_parser.airtable import (  # noqa: E402
    AirtableClient,
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_TITLE,
    FIELD_VIDEO_FOLDER,
)
from catalog_parser.auth import (
    SCOPES,
    get_docs_service,
    get_drive_service_noninteractive,
    get_service_account_credentials,
)
from catalog_parser.canva import build_canva_client_from_env, ensure_canva_ready
from catalog_parser.drive_thumbnail import (
    enrich_records_with_original_video_thumbnails,
    image_looks_empty,
)
from catalog_parser.runtime_env import materialize_credentials
from googleapiclient.discovery import build


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replace Airtable Original Video Thumbnail from Canva/Drive."
    )
    parser.add_argument(
        "record_ids",
        nargs="+",
        help="Airtable record ids to refresh",
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(encoding="utf-8")
    materialize_credentials(PROJECT_ROOT)
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    if sa_path.is_file():
        os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_FILE", str(sa_path))

    print("Canva:", ensure_canva_ready(project_root=PROJECT_ROOT))
    canva = build_canva_client_from_env(project_root=PROJECT_ROOT)
    drive = get_drive_service_noninteractive()
    sa = get_service_account_credentials(scopes=SCOPES)
    if sa is None:
        docs = get_docs_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, use_console=True)
    else:
        docs = build("docs", "v1", credentials=sa)
    airtable = AirtableClient(
        token=os.environ["AIRTABLE_TOKEN"].strip(),
        base_id=os.environ["AIRTABLE_BASE_ID"].strip(),
        table_name=os.environ["AIRTABLE_TABLE_NAME"].strip(),
    )

    rows = []
    for record_id in args.record_ids:
        record = airtable.get_record(record_id)
        fields = record.get("fields") or {}
        rows.append(
            {
                "id": record_id,
                "ctTitle": fields.get(FIELD_TITLE) or record_id,
                "ctLink": fields.get(FIELD_ORIGINAL_VIDEO) or "",
                "pkgLink": fields.get(FIELD_VIDEO_FOLDER) or "",
            }
        )

    with TemporaryDirectory(prefix="refresh-thumbs-") as tmp:
        enriched = enrich_records_with_original_video_thumbnails(
            rows,
            drive,
            docs,
            canva_client=canva,
            staging_dir=Path(tmp),
        )
        failures = 0
        for item in enriched:
            record_id = str(item["id"])
            title = item.get("ctTitle")
            path_value = item.get("_originalThumbnailPath")
            source = item.get("ytThumbnailSource")
            print(f"{record_id}\t{title}\tsource={source}")
            if not isinstance(path_value, str) or not path_value.strip():
                print("  ERROR: no original thumbnail staged")
                failures += 1
                continue
            path = Path(path_value)
            if image_looks_empty(path):
                print(f"  ERROR: staged file still looks empty ({path.stat().st_size} bytes)")
                failures += 1
                continue
            airtable.upload_attachment(
                record_id,
                FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                path,
                replace=True,
            )
            print(f"  uploaded {path.name} ({path.stat().st_size} bytes)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
