"""Upload thumbnails to Airtable using Drive TN rules.

1. Drive TN has English text -> upload flattened Drive TN.
2. Empty template -> fetch original-platform thumbnail; if same background,
   upload original-platform.
3. When that fails -> original-platform thumbnail with matching aspect ratio
   goes to the Drive review folder; one review email is sent per run.
4. On every run, approved review thumbnails are uploaded to Airtable and
   removed from Drive.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
import tempfile
from dataclasses import dataclass
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

from media_publisher.config import load_settings  # noqa: E402
from media_publisher.sources.airtable import (  # noqa: E402
    FIELD_ORIGINAL_VIDEO,
    FIELD_ORIGINAL_VIDEO_THUMBNAIL,
    FIELD_STATUS,
    AirtableClient,
    catalog_title,
)
from media_publisher.sources.airtable_thumbnail import (  # noqa: E402
    PDF_MIME,
    pick_root_thumbnail_marker,
)
from media_publisher.sources.google_drive import GoogleDriveClient  # noqa: E402
from media_publisher.sources.source_thumbnail import (  # noqa: E402
    SourceThumbnailError,
    fetch_original_video_thumbnail,
    original_thumbnail_destination,
)
from media_publisher.sources.tn_psd import (  # noqa: E402
    ImageSize,
    best_aspect_matches,
    collect_image_sizes,
    composite_without_text,
    resolve_psd_target,
    safe_cache_name,
)
from media_publisher.sources.thumbnail_review import (  # noqa: E402
    DEFAULT_REVIEW_FOLDER_URL,
    ReviewQueueItem,
    process_approved_review_thumbnails,
    review_drive_filename,
    send_review_notification_email,
    thumbnail_matches_reference_aspect,
    upload_review_thumbnail,
)
from media_publisher.sources.tn_publish import reference_thumbnail_size  # noqa: E402

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
TEXT_DIFF_THRESHOLD = 0.009
PHOTO_CROP_RATIO = 0.58
COMPARE_WIDTH = 540
SAME_BACKGROUND_THRESHOLD = 0.82
TEXT_REGION_RATIO = 0.45
EMPTY_TEMPLATE_TOP_SIMILARITY = 0.95


@dataclass(frozen=True)
class UploadPlan:
    record_id: str
    title: str
    export_path: Path
    drive_file: str
    action: str
    bg_score: float | None = None


def sanitize_filename(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", title).strip(" .")
    return cleaned or "thumbnail"


def flatten_psd_with_text(path: Path, reference: ImageSize | None) -> Image.Image | None:
    sizes = collect_image_sizes(path)
    if reference and sizes:
        matches = best_aspect_matches(reference, sizes)
        matched = matches[0] if matches else (sizes[0] if sizes else None)
    else:
        matched = sizes[0] if sizes else None
    if matched is None:
        return None
    target = resolve_psd_target(path, matched)
    image = target.composite()
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def flatten_marker(
    path: Path,
    mime: str,
    reference: ImageSize | None,
) -> Image.Image | None:
    suffix = path.suffix.casefold()
    if suffix == ".psd" or "photoshop" in mime.casefold():
        return flatten_psd_with_text(path, reference)
    try:
        return cache_pkgtn.flatten_drive_file(path, mime_type=mime)
    except Exception:
        return None


def flatten_without_text(
    path: Path,
    mime: str,
    reference: ImageSize | None,
) -> Image.Image | None:
    suffix = path.suffix.casefold()
    if suffix != ".psd" and "photoshop" not in mime.casefold():
        return None
    sizes = collect_image_sizes(path)
    if reference and sizes:
        matches = best_aspect_matches(reference, sizes)
        matched = matches[0] if matches else (sizes[0] if sizes else None)
    else:
        matched = sizes[0] if sizes else None
    if matched is None:
        return None
    return composite_without_text(resolve_psd_target(path, matched))


def mean_pixel_diff(left: Image.Image, right: Image.Image) -> float:
    if left.size != right.size:
        right = right.resize(left.size, Image.Resampling.LANCZOS)
    pixels_a = list(left.convert("RGB").getdata())
    pixels_b = list(right.convert("RGB").getdata())
    if not pixels_a:
        return 0.0
    total = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(pixels_a, pixels_b, strict=False):
        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
    return total / (len(pixels_a) * 3 * 255)


def crop_photo_region(image: Image.Image) -> Image.Image:
    width, height = image.size
    return image.crop((0, 0, width, max(1, int(height * PHOTO_CROP_RATIO))))


def normalize_for_compare(image: Image.Image) -> Image.Image:
    cropped = crop_photo_region(image.convert("RGB"))
    target_height = max(1, round(COMPARE_WIDTH * cropped.height / cropped.width))
    return cropped.resize((COMPARE_WIDTH, target_height), Image.Resampling.LANCZOS)


def photo_similarity(left: Image.Image, right: Image.Image) -> float:
    left_norm = normalize_for_compare(left)
    right_norm = normalize_for_compare(right)
    if left_norm.size != right_norm.size:
        right_norm = right_norm.resize(left_norm.size, Image.Resampling.LANCZOS)
    pixels_a = list(left_norm.getdata())
    pixels_b = list(right_norm.getdata())
    if not pixels_a:
        return 0.0
    total = 0
    for (r1, g1, b1), (r2, g2, b2) in zip(pixels_a, pixels_b, strict=False):
        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
    mean_diff = total / (len(pixels_a) * 3 * 255)
    return max(0.0, 1.0 - mean_diff)


def is_raster_template(path: Path, mime: str) -> bool:
    suffix = path.suffix.casefold()
    if "photoshop" in mime.casefold() or suffix == ".psd":
        return False
    return mime.startswith("image/") or suffix in {".jpg", ".jpeg", ".png", ".webp"}


def is_pdf_marker(path: Path, mime: str) -> bool:
    if mime == PDF_MIME:
        return True
    if path.suffix.casefold() == ".pdf":
        return True
    try:
        return path.read_bytes()[:4] == b"%PDF"
    except OSError:
        return False


def top_region_similarity(left: Image.Image, right: Image.Image) -> float:
    if left.size != right.size:
        right = right.resize(left.size, Image.Resampling.LANCZOS)
    width, height = left.size
    top_height = max(1, int(height * TEXT_REGION_RATIO))
    return 1.0 - mean_pixel_diff(
        left.crop((0, 0, width, top_height)),
        right.crop((0, 0, width, top_height)),
    )


def drive_image_has_text(
    full: Image.Image,
    plain: Image.Image | None,
    *,
    path: Path,
    mime: str,
    original: Image.Image | None = None,
) -> bool:
    if plain is not None:
        return mean_pixel_diff(full, plain) >= TEXT_DIFF_THRESHOLD
    if is_raster_template(path, mime) or is_pdf_marker(path, mime):
        if original is None:
            return False
        aligned = original.convert("RGB")
        if top_region_similarity(full, aligned) >= EMPTY_TEMPLATE_TOP_SIMILARITY:
            return False
        return photo_similarity(original, full) < SAME_BACKGROUND_THRESHOLD
    return False


def background_match_score(
    original: Image.Image,
    drive_image: Image.Image,
    plain: Image.Image | None,
) -> float:
    score_full = photo_similarity(original, drive_image)
    if plain is None:
        return score_full
    score_plain = photo_similarity(original, plain)
    return max(score_full, score_plain)


def save_upload_jpeg(image: Image.Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    rgb = image.convert("RGB")
    for quality in (92, 85, 78, 70, 62):
        rgb.save(destination, format="JPEG", quality=quality, optimize=True)
        if destination.stat().st_size <= MAX_UPLOAD_BYTES:
            return destination

    scaled = rgb
    for max_side in (2400, 1920, 1600, 1280, 1080):
        width, height = scaled.size
        if max(width, height) <= max_side:
            break
        if width >= height:
            new_size = (max_side, max(1, round(height * max_side / width)))
        else:
            new_size = (max(1, round(width * max_side / height)), max_side)
        scaled = scaled.resize(new_size, Image.Resampling.LANCZOS)
        scaled.save(destination, format="JPEG", quality=78, optimize=True)
        if destination.stat().st_size <= MAX_UPLOAD_BYTES:
            return destination

    raise RuntimeError(
        f"Could not compress {destination.name} under Airtable 5 MB limit"
    )


def load_original_platform_image(
    *,
    title: str,
    source_url: str,
    original_dir: Path,
) -> Image.Image | None:
    if not source_url.strip():
        return None
    original_path = original_thumbnail_destination(original_dir, title)
    try:
        fetch_original_video_thumbnail(source_url, original_path)
    except SourceThumbnailError:
        if not original_path.is_file():
            return None
    with Image.open(original_path) as image:
        return image.convert("RGB")


def resolve_reference_with_fallback(
    fields: dict,
    *,
    title: str,
    original_dir: Path,
    drive: GoogleDriveClient,
    folder_id: str,
) -> ImageSize | None:
    reference = reference_thumbnail_size(
        fields,
        title=title,
        original_dir=original_dir,
        drive=drive,
        folder_id=folder_id,
    )
    if reference is not None:
        return reference
    original_path = original_thumbnail_destination(original_dir, title)
    if original_path.is_file():
        with Image.open(original_path) as image:
            return ImageSize(image.size[0], image.size[1], "original-thumb")
    return None


def queue_review_fallback(
    *,
    record_id: str,
    title: str,
    reason: str,
    original_image: Image.Image | None,
    reference: ImageSize | None,
    export_dir: Path,
    skipped: list[tuple[str, str]],
) -> ReviewQueueItem | None:
    if original_image is None:
        skipped.append((title, f"{reason}; no original-platform thumbnail"))
        return None
    if reference is None:
        skipped.append((title, f"{reason}; no aspect-ratio reference"))
        return None
    if not thumbnail_matches_reference_aspect(
        original_image,
        reference_width=reference.width,
        reference_height=reference.height,
    ):
        skipped.append(
            (
                title,
                f"{reason}; aspect ratio mismatch "
                f"({original_image.size[0]}x{original_image.size[1]} vs "
                f"{reference.width}x{reference.height})",
            )
        )
        return None

    export_path = export_dir / review_drive_filename(title)
    try:
        save_upload_jpeg(original_image, export_path)
    except RuntimeError as exc:
        skipped.append((title, f"{reason}; {exc}"))
        return None

    return ReviewQueueItem(
        record_id=record_id,
        title=title,
        local_path=export_path,
        reason=reason,
    )


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upload to Airtable (default is dry-run)",
    )
    args = parser.parse_args()

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
    original_dir.mkdir(parents=True, exist_ok=True)
    export_dir = PROJECT_ROOT / "downloads" / "drive-tn-uploads"
    export_dir.mkdir(parents=True, exist_ok=True)

    records = airtable.list_records(filter_formula=audit.build_filter_formula())
    review_folder_id = settings.thumbnail_review_drive_folder_id
    approved_subfolder = settings.thumbnail_review_approved_subfolder

    approved_results = process_approved_review_thumbnails(
        airtable,
        drive,
        records,
        review_folder_id=review_folder_id,
        approved_subfolder=approved_subfolder,
        apply=args.apply,
    )

    folder_cache: dict[str, list] = {}
    planned: list[UploadPlan] = []
    review_queue: list[ReviewQueueItem] = []
    skipped: list[tuple[str, str]] = []
    failures: list[tuple[str, str]] = []

    with tempfile.TemporaryDirectory(prefix="drive-tn-upload-") as tmp:
        tmp_path = Path(tmp)
        for record in records:
            fields = record.fields
            if audit.status_bucket(fields.get(FIELD_STATUS)) is None:
                continue

            title = catalog_title(fields)
            folder_id = audit.parse_folder_id(fields.get("Video Folder"))
            if folder_id is None:
                continue

            children = folder_cache.setdefault(folder_id, drive.list_children(folder_id))
            marker = pick_root_thumbnail_marker(children)
            if marker is None:
                continue

            source_url = str(fields.get(FIELD_ORIGINAL_VIDEO) or "").strip()
            reference = resolve_reference_with_fallback(
                fields,
                title=title,
                original_dir=original_dir,
                drive=drive,
                folder_id=folder_id,
            )
            original_image = load_original_platform_image(
                title=title,
                source_url=source_url,
                original_dir=original_dir,
            )
            drive_path = tmp_path / safe_cache_name(marker.name)
            if not drive_path.exists():
                drive.download_file(marker.id, drive_path)

            drive_image = flatten_marker(drive_path, marker.mime_type, reference)
            if drive_image is None:
                original_path = original_thumbnail_destination(original_dir, title)
                if original_path.is_file():
                    with Image.open(original_path) as thumb_image:
                        ref_from_thumb = ImageSize(
                            thumb_image.size[0],
                            thumb_image.size[1],
                            "original-thumb",
                        )
                    drive_image = flatten_marker(
                        drive_path, marker.mime_type, ref_from_thumb
                    )
            if drive_image is None:
                review_item = queue_review_fallback(
                    record_id=record.id,
                    title=title,
                    reason="unreadable Drive TN",
                    original_image=original_image,
                    reference=reference,
                    export_dir=export_dir,
                    skipped=skipped,
                )
                if review_item is not None:
                    review_queue.append(review_item)
                continue

            plain = flatten_without_text(drive_path, marker.mime_type, reference)
            has_text = drive_image_has_text(
                drive_image,
                plain,
                path=drive_path,
                mime=marker.mime_type,
                original=original_image,
            )

            if has_text:
                action = "upload-drive-tn"
                export_name = f"{sanitize_filename(title)}.drive-tn.jpg"
                upload_image = drive_image
                bg_score = None
            else:
                if original_image is None:
                    review_item = queue_review_fallback(
                        record_id=record.id,
                        title=title,
                        reason="empty template, no original-platform source",
                        original_image=None,
                        reference=reference,
                        export_dir=export_dir,
                        skipped=skipped,
                    )
                    if review_item is not None:
                        review_queue.append(review_item)
                    continue
                bg_score = background_match_score(original_image, drive_image, plain)
                if bg_score < SAME_BACKGROUND_THRESHOLD:
                    review_item = queue_review_fallback(
                        record_id=record.id,
                        title=title,
                        reason=(
                            "empty template, different background "
                            f"(score={bg_score:.3f})"
                        ),
                        original_image=original_image,
                        reference=reference,
                        export_dir=export_dir,
                        skipped=skipped,
                    )
                    if review_item is not None:
                        review_queue.append(review_item)
                    continue
                action = "upload-original-platform"
                export_name = f"{sanitize_filename(title)}.original-platform.jpg"
                upload_image = original_image

            export_path = export_dir / export_name
            try:
                save_upload_jpeg(upload_image, export_path)
            except RuntimeError as exc:
                review_item = queue_review_fallback(
                    record_id=record.id,
                    title=title,
                    reason=f"export failed ({exc})",
                    original_image=original_image,
                    reference=reference,
                    export_dir=export_dir,
                    skipped=skipped,
                )
                if review_item is not None:
                    review_queue.append(review_item)
                else:
                    failures.append((title, str(exc)))
                continue

            planned.append(
                UploadPlan(
                    record_id=record.id,
                    title=title,
                    export_path=export_path,
                    drive_file=marker.name,
                    action=action,
                    bg_score=bg_score,
                )
            )

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Upload thumbnails to Airtable ({mode}) ===")
    print(f"Export dir: {export_dir}\n")

    if approved_results:
        print(f"--- Approved review thumbnails ({len(approved_results)}) ---")
        for item in sorted(approved_results, key=lambda row: row.title.casefold()):
            print(f"- {item.title} ({item.action})")
        print()

    for item in sorted(planned, key=lambda row: row.title.casefold()):
        size_kb = item.export_path.stat().st_size // 1024
        score_text = (
            f"  bg_score={item.bg_score:.3f}" if item.bg_score is not None else ""
        )
        print(f"PLAN {item.title}")
        print(f"     action: {item.action}{score_text}")
        print(f"     drive:  {item.drive_file}")
        print(f"     file:   {item.export_path.name} ({size_kb} KB)")

    if review_queue:
        print()
        print(f"--- Review queue ({len(review_queue)}) ---")
        for item in sorted(review_queue, key=lambda row: row.title.casefold()):
            size_kb = item.local_path.stat().st_size // 1024
            print(f"PLAN {item.title}")
            print(f"     action: queue-for-review")
            print(f"     reason: {item.reason}")
            print(f"     file:   {item.local_path.name} ({size_kb} KB)")

    if skipped:
        print()
        print(f"--- Skipped ({len(skipped)}) ---")
        for title, reason in sorted(skipped, key=lambda item: item[0].casefold()):
            print(f"- {title}: {reason}")

    print()
    print("=== Summary ===")
    print(f"Planned uploads: {len(planned)}")
    print(
        "  drive TN:           "
        f"{sum(1 for item in planned if item.action == 'upload-drive-tn')}"
    )
    print(
        "  original platform:  "
        f"{sum(1 for item in planned if item.action == 'upload-original-platform')}"
    )
    print(f"Review queue:    {len(review_queue)}")
    print(f"Skipped:         {len(skipped)}")
    print(f"Failed export:   {len(failures)}")
    print(f"Approved:        {len(approved_results)}")

    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to upload to Airtable.")
        return 1 if failures else 0

    applied = 0
    for item in planned:
        try:
            airtable.upload_attachment(
                item.record_id,
                FIELD_ORIGINAL_VIDEO_THUMBNAIL,
                item.export_path,
            )
            applied += 1
            print(f"OK   {item.title} ({item.action})")
        except Exception as exc:
            failures.append((item.title, str(exc)))
            print(f"FAIL {item.title}")
            print(f"     {exc}")

    review_uploaded = 0
    review_uploaded_items: list[ReviewQueueItem] = []
    for item in review_queue:
        try:
            upload_review_thumbnail(
                drive.drive_service,
                review_folder_id=review_folder_id,
                local_path=item.local_path,
                title=item.title,
            )
            review_uploaded += 1
            review_uploaded_items.append(item)
            print(f"REVIEW {item.title}")
        except Exception as exc:
            failures.append((item.title, f"review upload: {exc}"))
            print(f"FAIL REVIEW {item.title}")
            print(f"     {exc}")

    if review_uploaded_items and send_review_notification_email(
        review_uploaded_items,
        review_folder_url=DEFAULT_REVIEW_FOLDER_URL,
    ):
        print(f"EMAIL review notification sent ({review_uploaded} video(s))")
    elif review_uploaded:
        print("WARN review uploads succeeded but notification email was not sent")

    print()
    print(f"Applied: {applied}/{len(planned)}")
    print(f"Review uploads: {review_uploaded}/{len(review_queue)}")
    if failures:
        print()
        print("Failures:")
        for title, reason in failures:
            print(f"  - {title}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
