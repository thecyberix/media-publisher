"""Match Drive TN image aspect ratios to downloaded original thumbnails."""
from __future__ import annotations

import argparse
import math
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

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
DEFAULT_ORIGINAL_DIR = PROJECT_ROOT / "downloads" / "original-thumbnails"
DEFAULT_CACHE_DIR = PROJECT_ROOT / "downloads" / "tn-cache"
ASPECT_TOLERANCE = 0.02


def safe_cache_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")
    return cleaned or "tn-file"


@dataclass(frozen=True)
class ImageSize:
    width: int
    height: int
    source: str

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def label(self) -> str:
        return f"{self.width}x{self.height}"


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


def aspect_ratio_label(width: int, height: int) -> str:
    ratio = width / height
    candidates = {
        "16:9": 16 / 9,
        "9:16": 9 / 16,
        "4:5": 4 / 5,
        "1:1": 1.0,
        "4:3": 4 / 3,
        "3:4": 3 / 4,
    }
    best_name = f"{width}:{height}"
    best_delta = math.inf
    for name, target in candidates.items():
        delta = abs(ratio - target)
        if delta < best_delta:
            best_delta = delta
            best_name = name
    if best_delta <= ASPECT_TOLERANCE:
        return best_name
    return f"{ratio:.3f}"


def aspects_match(a: ImageSize, b: ImageSize) -> bool:
    return abs(a.aspect - b.aspect) <= ASPECT_TOLERANCE


def read_psd_header_size(data: bytes) -> ImageSize | None:
    if len(data) < 26 or data[:4] != b"8BPS":
        return None
    height, width = struct.unpack(">II", data[14:22])
    if width <= 0 or height <= 0:
        return None
    return ImageSize(width=width, height=height, source="psd-header")


def read_pillow_size(path: Path) -> ImageSize | None:
    try:
        with Image.open(path) as image:
            return ImageSize(width=image.size[0], height=image.size[1], source="pillow")
    except OSError:
        return None


def read_psd_tools_sizes(path: Path) -> list[ImageSize]:
    try:
        from psd_tools import PSDImage
    except ImportError:
        return []

    sizes: list[ImageSize] = []
    psd = PSDImage.open(path)
    artboards = [
        layer
        for layer in psd
        if type(layer).__name__ == "Artboard" or getattr(layer, "kind", "") == "artboard"
    ]
    if artboards:
        for layer in artboards:
            width = int(layer.width)
            height = int(layer.height)
            if width <= 0 or height <= 0:
                continue
            name = getattr(layer, "name", "artboard")
            sizes.append(
                ImageSize(width=width, height=height, source=f"artboard:{name}")
            )
        return sizes

    sizes.append(
        ImageSize(width=psd.width, height=psd.height, source="psd-tools:document")
    )
    return sizes


def collect_image_sizes(path: Path) -> list[ImageSize]:
    suffix = path.suffix.casefold()
    sizes: list[ImageSize] = []

    if suffix == ".psd":
        sizes.extend(read_psd_tools_sizes(path))
        if not sizes:
            data = path.read_bytes()
            header = read_psd_header_size(data)
            if header is not None:
                sizes.append(header)

    pillow = read_pillow_size(path)
    if pillow is not None and all(
        item.width != pillow.width or item.height != pillow.height for item in sizes
    ):
        sizes.append(pillow)

    deduped: list[ImageSize] = []
    seen: set[tuple[int, int, str]] = set()
    for item in sizes:
        key = (item.width, item.height, item.source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def best_matches(
    original: ImageSize,
    candidates: list[ImageSize],
) -> list[ImageSize]:
    return [item for item in candidates if aspects_match(original, item)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--original-dir",
        type=Path,
        default=DEFAULT_ORIGINAL_DIR,
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help="Where to cache downloaded TN files from Drive",
    )
    args = parser.parse_args()

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
        images = [
            child
            for child in folder_cache[folder_id]
            if is_image_file(child.name, child.mime_type)
        ]
        if not images:
            continue
        title = catalog_title(fields)
        targets.append(
            {
                "title": title,
                "status": bucket,
                "type": str(fields.get(FIELD_TYPE) or ""),
                "images": images,
            }
        )

    print(f"=== Aspect ratio match ({len(targets)} pilot videos) ===")
    print(f"Original dir: {args.original_dir.resolve()}")
    print(f"Tolerance:    ±{ASPECT_TOLERANCE}")
    print()

    matched_videos = 0
    missing_original = 0
    no_aspect_match = 0

    for item in sorted(targets, key=lambda row: (row["status"], row["title"])):
        title = item["title"]
        original_path = original_thumbnail_destination(args.original_dir, title)
        print(f"{title}")
        print(f"  status: {item['status']} | type: {item['type']}")

        if not original_path.exists():
            missing_original += 1
            print(f"  original: MISSING ({original_path.name})")
            print("  match:    skipped")
            print()
            continue

        original_size = read_pillow_size(original_path)
        if original_size is None:
            missing_original += 1
            print(f"  original: unreadable ({original_path.name})")
            print("  match:    skipped")
            print()
            continue

        original_size = ImageSize(
            width=original_size.width,
            height=original_size.height,
            source="original-thumb",
        )
        print(
            f"  original: {original_size.label} "
            f"({aspect_ratio_label(original_size.width, original_size.height)})"
        )

        tn_files = sorted(item["images"], key=lambda child: child.name.casefold())
        file_names = ", ".join(child.name for child in tn_files)
        print(f"  drive:    {file_names}")

        all_candidates: list[ImageSize] = []
        for child in tn_files:
            cache_path = args.cache_dir / safe_cache_name(child.name)
            if not cache_path.exists():
                drive.download_file(child.id, cache_path)
            sizes = collect_image_sizes(cache_path)
            if not sizes:
                print(f"    {child.name}: could not read dimensions")
                continue
            for size in sizes:
                all_candidates.append(
                    ImageSize(
                        width=size.width,
                        height=size.height,
                        source=f"{child.name} [{size.source}]",
                    )
                )

        matches = best_matches(original_size, all_candidates)
        if matches:
            matched_videos += 1
            print("  match:    YES")
            for match in matches:
                print(
                    f"    - {match.source}: {match.label} "
                    f"({aspect_ratio_label(match.width, match.height)})"
                )
        else:
            no_aspect_match += 1
            print("  match:    NO")
            if all_candidates:
                print("  candidates:")
                for candidate in all_candidates:
                    print(
                        f"    - {candidate.source}: {candidate.label} "
                        f"({aspect_ratio_label(candidate.width, candidate.height)})"
                    )
            else:
                print("    (no readable TN dimensions)")
        print()

    print("=== Summary ===")
    print(f"Videos checked:          {len(targets)}")
    print(f"Original thumb present:  {len(targets) - missing_original}")
    print(f"Aspect ratio matched:    {matched_videos}")
    print(f"No aspect ratio match:   {no_aspect_match}")
    print(f"Missing original thumb:  {missing_original}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
