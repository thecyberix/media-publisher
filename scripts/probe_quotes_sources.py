"""Probe Canva templates, Google Sheets quotes, and Drive backgrounds."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from media_publisher.config import load_settings
from media_publisher.__main__ import canva_client_from_settings, PROJECT_ROOT as ROOT
from media_publisher.sources.google_drive import GoogleDriveClient
from media_publisher.sources.google_sheets import GoogleSheetsClient


def probe_canva() -> None:
    settings = load_settings()
    client = canva_client_from_settings(settings)
    designs = {
        "fbyt": "DAG3WawoBjI",
        "ig": "DAG3WUCy8VA",
    }
    for variant, design_id in designs.items():
        design = client.get_design(design_id)
        pages = client.list_design_pages_info(design_id)
        print(f"[canva:{variant}] {design.title!r} ({design_id}) — {len(pages)} page(s)")


def probe_sheets(*, year: int = 2026, month: int = 7) -> None:
    from media_publisher.sources.quotes_config import load_quotes_sources_config

    config = load_quotes_sources_config(PROJECT_ROOT / "config" / "quotes_sources.json")
    spreadsheet_id = config.spreadsheet_id

    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    client = GoogleSheetsClient.from_service_account(sa_path)
    tab = client.resolve_sheet_tab_for_month(spreadsheet_id, year=year, month=month)
    print(f"[sheets] {year}-{month:02d} tab: {tab.title!r} (gid={tab.sheet_id})")

    escaped = tab.title.replace("'", "''")
    rows = client.get_values(spreadsheet_id, f"'{escaped}'!A1:H20")
    for index, row in enumerate(rows, start=1):
        print(f"  {index:02d}: {row}")


def probe_drive(*, year: int = 2026, month: int = 7) -> None:
    config_path = PROJECT_ROOT / "config" / "quotes_sources.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    drive_config = config["backgrounds_drive"]
    root_id = drive_config["root_folder_id"]

    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    client = GoogleDriveClient.from_service_account(sa_path)

    month_folder = client.resolve_month_background_folder(
        root_folder_id=root_id,
        year=year,
        month=month,
        year_folder_pattern=drive_config["year_folder_pattern"],
        month_folder_pattern=drive_config["month_folder_pattern"],
    )
    print(
        f"[drive] {year}-{month:02d} folder: {month_folder.name!r} ({month_folder.id})"
    )

    variants = drive_config.get("variants", {})
    if not isinstance(variants, dict):
        raise RuntimeError("backgrounds_drive.variants must be an object")

    for variant, variant_config in variants.items():
        if not isinstance(variant_config, dict):
            continue
        subdir = variant_config.get("subdir")
        backgrounds = client.list_quote_backgrounds(
            month_folder_id=month_folder.id,
            variant=variant,
            subdir=subdir if isinstance(subdir, str) and subdir.strip() else None,
            month=month,
        )
        location = month_folder.name if not subdir else f"{month_folder.name}/{subdir}"
        print(f"  [{variant}] {len(backgrounds)} image(s) in {location}")
        for image in backgrounds[:5]:
            print(f"    day {image.day:02d}: {image.name}")
        if len(backgrounds) > 5:
            print(f"    ... and {len(backgrounds) - 5} more")


def main() -> int:
    _configure_stdio()
    print("=== Canva templates ===")
    probe_canva()
    print("\n=== Quotes sheet ===")
    probe_sheets()
    print("\n=== Drive backgrounds ===")
    probe_drive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
