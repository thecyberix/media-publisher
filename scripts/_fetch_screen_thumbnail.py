"""Save a package thumbnail from the public Canva /screen image.

Given a Video Folder URL or id, find the Canva design the same way ingest does,
open its /screen URL in Chromium, and write that image. This does not use the
Canva export API and does not save the largest file embedded in the share page.

Example:
  python scripts/_fetch_screen_thumbnail.py "https://drive.google.com/drive/folders/FOLDER_ID"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from catalog_parser.auth import SCOPES, get_service_account_credentials
from catalog_parser.drive_docs import extract_drive_folder_id, read_drive_fields_from_folder
from catalog_parser.drive_thumbnail import _discover_canva_url, image_looks_empty
from media_publisher.sources.canva_share_preview import (
    _screen_url,
    download_screen_with_chromium,
)
from media_publisher.sources.canva import resolve_canva_url


def ensure_service_account() -> None:
    """Use the same service-account file as the other local Drive scripts."""
    if (
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    ):
        return
    sa_path = PROJECT_ROOT / "credentials" / "google-sheets-service-account.json"
    if sa_path.is_file():
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(sa_path)


def google_services():
    from googleapiclient.discovery import build

    ensure_service_account()
    creds = get_service_account_credentials(scopes=SCOPES)
    if creds is None:
        raise RuntimeError(
            "No Google service account found. Set GOOGLE_SERVICE_ACCOUNT_FILE "
            "or add credentials/google-sheets-service-account.json."
        )
    return (
        build("drive", "v3", credentials=creds),
        build("docs", "v1", credentials=creds),
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def folder_label(drive_service, folder_id: str) -> str:
    metadata = (
        drive_service.files()
        .get(fileId=folder_id, fields="name", supportsAllDrives=True)
        .execute()
    )
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return folder_id


def safe_stem(name: str) -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*' else char for char in name).strip(" .")
    return cleaned or "thumbnail"


def download_screen_image(screen_url: str, destination: Path, *, headed: bool) -> str:
    """Open /screen in Chromium and save the image response."""
    return download_screen_with_chromium(screen_url, destination, headed=headed)


def extension_for_content_type(content_type: str) -> str:
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"
    return ".jpg"


def fetch_one(
    drive_service,
    docs_service,
    folder_value: str,
    *,
    output: Path | None,
    output_dir: Path,
    headed: bool,
) -> Path:
    folder_id = extract_drive_folder_id(folder_value)
    if folder_id is None:
        raise RuntimeError(f"Could not parse a Drive folder id from {folder_value!r}")

    label = folder_label(drive_service, folder_id)
    print(f"Folder: {label}")
    print(f"  id: {folder_id}")

    try:
        fields = read_drive_fields_from_folder(drive_service, docs_service, folder_id)
    except Exception:
        fields = {}
    canva_url = _discover_canva_url(
        drive_service,
        docs_service,
        folder_id,
        fields,
    )
    if not canva_url:
        raise RuntimeError(f"No Canva design link in folder {label!r}")

    resolved = resolve_canva_url(canva_url)
    screen_url = _screen_url(resolved)
    print(f"  canva: {resolved}")
    print(f"  screen: {screen_url}")

    if output is None:
        destination = output_dir / f"{safe_stem(label)}.screen"
    else:
        destination = output
    # Extension is filled in after Chromium reports the content type.
    placeholder = destination
    content_type = download_screen_image(
        screen_url,
        placeholder.with_suffix(".part"),
        headed=headed,
    )
    final_path = placeholder.with_suffix(extension_for_content_type(content_type))
    part_path = placeholder.with_suffix(".part")
    part_path.replace(final_path)

    from PIL import Image

    with Image.open(final_path) as image:
        width, height = image.size
    empty = image_looks_empty(final_path)
    print(f"  saved: {final_path}")
    print(f"  size: {width}x{height}  empty={empty}")
    if empty:
        raise RuntimeError(f"Saved image looks empty: {final_path}")
    return final_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "folders",
        nargs="+",
        help="Drive folder URL or id for one or more video packages",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path when a single folder is given (extension follows the image type)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "screen-thumbs",
        help="Directory for saved images (default: output/screen-thumbs)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the Chromium window",
    )
    return parser


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    if args.output is not None and len(args.folders) != 1:
        print("--output applies to a single folder", file=sys.stderr)
        return 2

    drive_service, docs_service = google_services()

    failures = 0
    for folder_value in args.folders:
        try:
            fetch_one(
                drive_service,
                docs_service,
                folder_value,
                output=args.output,
                output_dir=args.output_dir,
                headed=args.headed,
            )
        except Exception as exc:
            failures += 1
            print(f"  failed: {exc}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
