"""Alert when HappyScribe review subfolders still have transcriptions."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from media_publisher.sources.happyscribe import (  # noqa: E402
    HAPPYSCRIBE_REVIEW_SUBFOLDER_NAMES,
    HappyScribeClient,
    HappyScribeError,
    HappyScribeLibraryLocation,
    HappyScribeTranscription,
    library_folder_url,
    resolve_review_video_folders,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NONEMPTY = 2


def folder_display_url(location: HappyScribeLibraryLocation) -> str:
    return library_folder_url(location.organization_id, location.folder_id)


def build_alert_body(
    folders: list[tuple[HappyScribeLibraryLocation, list[HappyScribeTranscription]]],
) -> str:
    nonempty = [(location, items) for location, items in folders if items]
    heading = (
        "The watched HappyScribe library folder is not empty."
        if len(nonempty) == 1
        else "The watched HappyScribe library folders are not empty."
    )
    lines = [heading, ""]
    for location, transcriptions in nonempty:
        name = location.folder_name or location.folder_id
        lines.extend(
            [
                f"Folder: {name}",
                f"Library: {folder_display_url(location)}",
                f"Items: {len(transcriptions)}",
                "",
                "Transcriptions:",
            ]
        )
        for item in transcriptions:
            title = item.name.strip() or item.id
            lines.append(f"  - {title} ({item.id})")
        lines.append("")
    lines.append("Clear or move these items in HappyScribe when ready.")
    return "\n".join(lines) + "\n"


def check_library(
    *,
    api_key: str,
    location: HappyScribeLibraryLocation,
) -> list[HappyScribeTranscription]:
    client = HappyScribeClient(api_key=api_key)
    return client.list_library_transcriptions(location)


def resolve_watch_folders(
    *,
    api_key: str,
    review_url: str,
) -> list[HappyScribeLibraryLocation]:
    client = HappyScribeClient(api_key=api_key)
    return resolve_review_video_folders(client, review_url)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Exit non-zero when HappyScribe review subfolders still have transcriptions."
        )
    )
    parser.add_argument(
        "--review-url",
        default=os.getenv("HAPPYSCRIBE_REVIEW_URL", "").strip(),
        help="Parent HappyScribe library URL (default: HAPPYSCRIBE_REVIEW_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("HAPPYSCRIBE_API_KEY", "").strip(),
        help="HappyScribe API key (default: HAPPYSCRIBE_API_KEY).",
    )
    parser.add_argument(
        "--skip-if-missing",
        action="store_true",
        help="Exit 0 when HAPPYSCRIBE_API_KEY is not configured.",
    )
    parser.add_argument(
        "--body-file",
        type=Path,
        help="Write a notification email body when a folder is non-empty.",
    )
    args = parser.parse_args()

    api_key = (args.api_key or "").strip()
    review_url = (args.review_url or "").strip()
    if not review_url:
        print("SKIP: HAPPYSCRIBE_REVIEW_URL not configured")
        return EXIT_OK
    if not api_key:
        if args.skip_if_missing:
            print("SKIP: HAPPYSCRIBE_API_KEY not configured")
            return EXIT_OK
        print("Missing HAPPYSCRIBE_API_KEY", file=sys.stderr)
        return EXIT_ERROR

    try:
        watch_folders = resolve_watch_folders(api_key=api_key, review_url=review_url)
    except HappyScribeError as exc:
        print(f"HappyScribe library check failed: {exc}", file=sys.stderr)
        return EXIT_ERROR

    results: list[tuple[HappyScribeLibraryLocation, list[HappyScribeTranscription]]] = []
    failed = False
    for location in watch_folders:
        url = folder_display_url(location)
        try:
            transcriptions = check_library(api_key=api_key, location=location)
        except HappyScribeError as exc:
            print(f"HappyScribe library check failed: {url}: {exc}", file=sys.stderr)
            failed = True
            continue
        results.append((location, transcriptions))

    nonempty = [(location, items) for location, items in results if items]
    if nonempty:
        total = sum(len(items) for _location, items in nonempty)
        print(
            f"ALERT: HappyScribe library has {total} item(s) across "
            f"{len(nonempty)} folder(s)"
        )
        for location, transcriptions in nonempty:
            name = location.folder_name or location.folder_id
            print(f"  {name}: {folder_display_url(location)}")
            for item in transcriptions:
                print(f"    - {item.name.strip() or item.id}")

        body = build_alert_body(nonempty)
        if args.body_file is not None:
            body_path = args.body_file
            if not body_path.is_absolute():
                body_path = REPO_ROOT / body_path
            body_path.parent.mkdir(parents=True, exist_ok=True)
            body_path.write_text(body, encoding="utf-8")
            print(f"Wrote alert body to {body_path}")

        return EXIT_NONEMPTY

    if failed:
        return EXIT_ERROR

    watched = ", ".join(HAPPYSCRIBE_REVIEW_SUBFOLDER_NAMES)
    for location, _items in results:
        name = location.folder_name or location.folder_id
        print(f"OK: HappyScribe library is empty ({name}: {folder_display_url(location)})")
    if not results:
        print(f"OK: HappyScribe review folders are empty ({watched})")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
